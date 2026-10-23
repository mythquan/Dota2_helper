"""Local Dota 2 GSI coach. Uses only data posted by the game client."""

from __future__ import annotations

import argparse
import json
import math
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.request import ProxyHandler, Request, build_opener, urlopen
from urllib.error import HTTPError, URLError
from localization import Localizer


GAME_STATE = "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"
MAJOR_ITEMS = {"item_blink", "item_black_king_bar", "item_orchid", "item_sheepstick"}
MIN_GAP_SECONDS = 15
REGULAR_GAP_SECONDS = 45
MAX_BODY_BYTES = 1_000_000
AI_DEFAULT_MODELS = {
    "openrouter": "deepseek/deepseek-v4.1-flash-1017",
    "deepseek": "deepseek-flash",
    "openai": "gpt-6-luna",
}
AI_ENDPOINTS = {
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
    "deepseek": "https://api.deepseek.com/chat/completions",
    "openai": "https://api.openai.com/v1/responses",
}


def open_model_request(request: Request, timeout: int = 12):
    """Ignore the local port-9 placeholder proxy used by some launch environments."""
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
    if proxy.lower().rstrip("/") in ("http://127.0.0.1:9", "http://localhost:9"):
        return build_opener(ProxyHandler({})).open(request, timeout=timeout)
    return urlopen(request, timeout=timeout)


def obj(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def number(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (ValueError, TypeError, OverflowError):
        return default


@dataclass(frozen=True)
class State:
    match_id: str
    clock: int
    hero: str
    team: str
    xpos: int | None
    ypos: int | None
    last_hits: int | None
    level: int
    alive: bool | None
    hp_percent: int | None
    mana_percent: int | None
    kills: int
    deaths: int
    assists: int
    gold: int | None
    net_worth: int | None
    gpm: int | None
    items: tuple[str, ...]
    ultimate: str
    ultimate_cooldown: int | None


@dataclass(frozen=True)
class ModelResult:
    message: str | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    model: str | None
    error: str | None = None


def parse_state(payload: dict) -> State | None:
    """Unknown or missing GSI fields stay unknown; never turn them into facts."""
    game_map = obj(payload.get("map"))
    if game_map.get("game_state") != GAME_STATE:
        return None
    clock = number(game_map.get("clock_time"), -1)
    if clock < 0:
        return None
    player = obj(payload.get("player"))
    hero = obj(payload.get("hero"))
    items = obj(payload.get("items"))
    abilities = obj(payload.get("abilities"))

    def percent(key: str, raw: str, maximum: str) -> int | None:
        if key in hero:
            return max(0, min(100, number(hero[key])))
        if raw in hero and maximum in hero and number(hero[maximum]) > 0:
            return max(0, min(100, round(100 * number(hero[raw]) / number(hero[maximum]))))
        return None

    inventory = tuple(
        v["name"] for k, value in items.items()
        if k.startswith("slot") and k[4:].isdigit() and number(k[4:]) < 6
        and (v := obj(value)).get("name") not in (None, "", "empty", "item_empty")
    )
    ult_candidates = [obj(value) for value in abilities.values() if obj(value).get("ultimate") is True]
    learned = [v for v in ult_candidates if number(v.get("level")) > 0]
    if not ult_candidates:
        ultimate, cooldown = "unknown", None
    elif not learned:
        ultimate, cooldown = "unlearned", None
    elif len(learned) != 1 or "cooldown" not in learned[0]:
        ultimate, cooldown = "unknown", None
    else:
        cooldown = max(0, number(learned[0]["cooldown"]))
        ultimate = "cooling" if cooldown else "ready"

    alive_value = hero.get("alive")
    match_id_value = game_map.get("matchid") if "matchid" in game_map else game_map.get("match_id")
    return State(
        match_id=str(match_id_value) if match_id_value is not None else "",
        clock=clock,
        hero=str(hero.get("name") or ""),
        team=str(player.get("team_name") or ""),
        xpos=number(hero["xpos"]) if "xpos" in hero else None,
        ypos=number(hero["ypos"]) if "ypos" in hero else None,
        last_hits=number(player["last_hits"]) if "last_hits" in player else None,
        level=number(hero.get("level")),
        alive=alive_value if isinstance(alive_value, bool) else None,
        hp_percent=percent("health_percent", "health", "max_health"),
        mana_percent=percent("mana_percent", "mana", "max_mana"),
        kills=number(player.get("kills")),
        deaths=number(player.get("deaths")),
        assists=number(player.get("assists")),
        gold=number(player["gold"]) if "gold" in player else None,
        net_worth=number(player["net_worth"]) if "net_worth" in player else None,
        gpm=number(player["gpm"]) if "gpm" in player else None,
        items=inventory,
        ultimate=ultimate,
        ultimate_cooldown=cooldown,
    )


def map_region(x: int | None, y: int | None) -> str:
    """Map a precise GSI point to a coarse named region on the current map."""
    if x is None or y is None:
        return "unknown"
    if x < -4700 and y < -4700:
        return "radiant_base"
    if x > 4700 and y > 4700:
        return "dire_base"
    if abs(x - y) < 1400 and abs(x) < 3800 and abs(y) < 3800:
        return "mid"
    if (x < -3800 and y > -4500) or (y > 3800 and x < 4500):
        return "top"
    if (y < -3800 and x > -4500) or (x > 3800 and y < 4500):
        return "bottom"
    if abs(x + y) < 1000 and abs(x) < 4500 and abs(y) < 4500:
        return "river"
    if x + y < -1800:
        return "radiant_side"
    if x + y > 1800:
        return "dire_side"
    return "other"


def visible_allies(payload: dict, state: State) -> list[dict]:
    """Read only allied hero icons, not generic circles or enemy markers."""
    team_id = {"radiant": 2, "dire": 3}.get(state.team.lower())
    if team_id is None:
        return []
    allies = []
    seen = set()
    for entry in obj(payload.get("minimap")).values():
        marker = obj(entry)
        name = str(marker.get("unitname") or marker.get("name") or "")
        if (marker.get("image") != "minimap_herocircle" or
                number(marker.get("team"), -1) != team_id or
                not name.startswith("npc_dota_hero_") or name == state.hero or
                name in seen or "xpos" not in marker or "ypos" not in marker):
            continue
        x, y = number(marker["xpos"]), number(marker["ypos"])
        if not -10000 <= x <= 10000 or not -10000 <= y <= 10000:
            continue
        seen.add(name)
        allies.append({"hero": name, "xpos": x, "ypos": y,
                       "region_inferred": map_region(x, y),
                       "distance_map_units": round(math.hypot(x - state.xpos, y - state.ypos))
                       if state.xpos is not None and state.ypos is not None else None})
    return allies


def visible_enemies(payload: dict, state: State) -> list[dict]:
    """Enemy icons currently reported by the player's GSI minimap."""
    enemy_team = {"radiant": 3, "dire": 2}.get(state.team.lower())
    if enemy_team is None:
        return []
    enemies = []
    seen = set()
    for entry in obj(payload.get("minimap")).values():
        marker = obj(entry)
        name = str(marker.get("unitname") or marker.get("name") or "")
        if (marker.get("image") != "minimap_enemyicon" or
                number(marker.get("team"), -1) != enemy_team or
                not name.startswith("npc_dota_hero_") or name in seen or
                "xpos" not in marker or "ypos" not in marker):
            continue
        x, y = number(marker["xpos"]), number(marker["ypos"])
        if not -10000 <= x <= 10000 or not -10000 <= y <= 10000:
            continue
        seen.add(name)
        enemies.append({"hero": name, "xpos": x, "ypos": y,
                        "region_inferred": map_region(x, y)})
    return enemies


def lane_type(region: str, team: str) -> str:
    if region == "mid":
        return "mid"
    if team == "radiant":
        return {"top": "offlane", "bottom": "safelane"}.get(region, "unknown")
    if team == "dire":
        return {"top": "safelane", "bottom": "offlane"}.get(region, "unknown")
    return "unknown"


def infer_role(lane: str, role_group: str | None) -> int | None:
    """Conventional position for an explicitly chosen core/support and observed lane."""
    return {
        ("safelane", "core"): 1,
        ("mid", "core"): 2,
        ("offlane", "core"): 3,
        ("offlane", "support"): 4,
        ("safelane", "support"): 5,
    }.get((lane, role_group))


def request_ai_advice(state: State | None, reason: str, role: int | None, model: str, api_key: str,
                      localizer: Localizer | None = None, role_group: str | None = None,
                      role_source: str = "unknown", provider: str = "openai",
                      context: dict | None = None, question: str | None = None) -> ModelResult:
    """Send only selected official GSI fields and the user's optional role."""
    fields = asdict(state) if state is not None else {}
    fields.pop("match_id", None)
    fields["role_1_to_5"] = role
    fields["role_source"] = role_source
    fields["role_group_supplied_by_player"] = role_group
    fields["trigger"] = reason
    if context:
        fields.update(context)
    if localizer is not None and state is not None:
        fields["hero_schinese"] = localizer.hero(state.hero)
        fields["items_schinese"] = [localizer.item(item) for item in state.items]
    instructions = (
        "你是 Dota 2 新手的局中教练。依据下方官方 GSI 状态和明确标记的本地推断，"
        "给出接下来约一分钟最值得做的一件事。用简体中文，30到70字，直接说行动、依据和风险。"
        "结合英雄、核心/辅助、开局分路、血蓝、大招、装备、时间、补刀与近期变化；"
        "信息不全时只用已知信息，别套用固定句式，也别重复上一条建议。"
        "teammates_from_gsi_minimap 若非空，包含队友英雄的小地图坐标，可用来判断队友大致在哪；"
        "但看不到队友血蓝、技能冷却、是否愿意支援，直线距离也不能证明能及时赶到。"
        "GSI 看不到完整敌人动向、兵线安全、实际视野和具体战斗结果；"
        "不可假装看见它们，不可捏造版本机制或装备购买金额。"
        "涉及无法观测的状态时明确说未知。只输出一条建议。"
    )
    if question is not None:
        fields["player_question"] = question
        instructions = (
            "你是 Dota 2 新手的局中教练。请用简体中文直接回答 player_question，"
            "结合所给官方 GSI 数据、玩家指定位置及明确标记的本地推断。"
            "如果没有实时数据，明确说明当前没有对局状态。"
            "不要假装看见兵线、敌人真实位置、实际视野、队友血蓝或技能。"
            "不确定的机制或版本信息要说明不确定；回答尽量简洁、具体。"
            "可用简短 Markdown 小标题、列表和加粗帮助阅读，不要输出 HTML。"
        )
    instructions += (
        "player_reported_match_status 与 player_reported_context 是玩家手动提供的补充，"
        "并非官方 GSI 字段；可作为线索，但要与实时 GSI 区分，不能据此捏造未观测事实。"
        "match_roster_from_observed_gsi 是本局累计识别的英雄名单，"
        "其中敌方英雄只在其小地图图标曾被官方 GSI 报告后才加入；"
        "它不表示这些敌人此刻仍可见。缺失的阵容位置保持未知，不能补全猜测。"
        "visible_enemies_from_gsi_minimap 是本次请求时小地图报告露头的敌方英雄及坐标推断区域，"
        "只代表该时间点的近似区域，不保证回答展示时仍在原地；"
        "空列表表示本次未识别到露头图标，null 表示未收到小地图数据。"
        "不要把这份名单里的英雄说成位置未知；名单外敌人的当前去向仍未知。"
        "英雄是否偏后期是根据阵容的战术判断，不是 GSI 的官方属性；"
        "不要把曾露头位置当成当前位置，也不要推断未知装备和发育程度。"
    )
    if provider == "openai":
        body = {"model": model, "store": False, "max_output_tokens": 600 if question is not None else 300,
                "instructions": instructions, "input": json.dumps(fields, ensure_ascii=False)}
    else:
        body = {"model": model, "max_completion_tokens": 700 if provider == "openrouter" else 600 if question is not None else 250,
                "stream": False,
                "messages": [{"role": "system", "content": instructions},
                             {"role": "user", "content": json.dumps(fields, ensure_ascii=False)}]}
        if provider == "openrouter":
            body["reasoning_effort"] = "none"
    request = Request(
        AI_ENDPOINTS[provider],
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
        method="POST",
    )
    with open_model_request(request, timeout=12) as response:
        result = json.load(response)
    if provider == "openai":
        text_parts = [
            part.get("text", "")
            for output in result.get("output", []) if output.get("type") == "message"
            for part in output.get("content", []) if part.get("type") == "output_text"
        ]
        message = "".join(text_parts)
    else:
        message = obj(result.get("choices", [{}])[0].get("message")).get("content") or ""
    message = " ".join(message.strip().split())
    usage = obj(result.get("usage"))

    def tokens(*names: str) -> int | None:
        for name in names:
            value = usage.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
        return None

    input_tokens = tokens("input_tokens", "prompt_tokens")
    output_tokens = tokens("output_tokens", "completion_tokens")
    total_tokens = tokens("total_tokens")
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    finish = (result.get("choices") or [{}])[0].get("finish_reason") if provider != "openai" else result.get("status")
    max_chars = 1200 if question is not None else 150
    error = f"回答正文为空或过长（{finish}）" if not message or len(message) > max_chars else None
    return ModelResult(message if error is None else None, input_tokens, output_tokens,
                       total_tokens, str(result.get("model") or model), error)


class Coach:
    def __init__(self, role: int | None, voice: bool, ai_model: str | None = None, api_key: str = "",
                 interval: int = REGULAR_GAP_SECONDS, localizer: Localizer | None = None,
                 ai_provider: str = "openai", memory_path: Path | None = None,
                 manual_path: Path | None = None):
        self.role = role
        self.role_group: str | None = None
        self.voice = voice
        self.ai_model = ai_model
        self.api_key = api_key
        self.ai_provider = ai_provider
        self.ai_error: str | None = None
        self.message_source = "waiting"
        self.last_model_state: State | None = None
        self.last_model_message: str | None = None
        self.last_response_model: str | None = None
        self.last_token_usage: dict[str, int | None] | None = None
        self.session_token_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        self.usage_reported_calls = 0
        self.interval = interval
        self.localizer = localizer or Localizer()
        self.lock = threading.Lock()
        self.previous: State | None = None
        self.latest: State | None = None
        self.last_received = 0.0
        self.last_packet = 0.0
        self.gsi_game_state = ""
        self.gsi_clock: int | None = None
        self.gsi_schema: dict[str, list[str]] = {}
        self.gsi_nested_schema: dict[str, list[str]] = {}
        self.minimap_diagnostic: dict = {"count": 0, "examples": []}
        self.allies: list[dict] | None = None
        self.enemies: list[dict] | None = None
        self.memory_path = memory_path
        self.manual_path = manual_path
        self.manual_game_status: str | None = None
        self.manual_context_note = ""
        self.manual_end_match_id = ""
        self.manual_end_clock: int | None = None
        self.manual_seen_new_pregame = False
        self.manual_new_match_id = ""
        self.manual_new_clock: int | None = None
        self.manual_seen_new_selection = False
        if manual_path is not None and manual_path.is_file():
            try:
                saved_manual = json.loads(manual_path.read_text(encoding="utf-8"))
                if isinstance(saved_manual, dict):
                    phase = saved_manual.get("status")
                    note = saved_manual.get("note")
                    self.manual_game_status = phase if phase in ("new_match", "ended") else None
                    self.manual_context_note = note if isinstance(note, str) and len(note) <= 300 else ""
                    self.manual_end_match_id = str(saved_manual.get("ended_match_id") or "")
                    clock = saved_manual.get("ended_clock")
                    self.manual_end_clock = clock if isinstance(clock, int) and not isinstance(clock, bool) else None
                    self.manual_new_match_id = str(saved_manual.get("new_match_id") or "")
                    new_clock = saved_manual.get("new_clock")
                    self.manual_new_clock = new_clock if isinstance(new_clock, int) and not isinstance(new_clock, bool) else None
            except (OSError, ValueError, TypeError):
                pass
        self.roster_match_id = ""
        self.roster_team = ""
        self.roster_hero = ""
        self.roster_last_clock = -1
        self.roster_allies: dict[str, int] = {}
        self.roster_enemies: dict[str, int] = {}
        self.gpm_history: dict[int, int] = {}
        if memory_path is not None and memory_path.is_file():
            try:
                saved = json.loads(memory_path.read_text(encoding="utf-8"))
                if (isinstance(saved, dict) and isinstance(saved.get("match_id"), str)
                        and saved["match_id"] != ""
                        and isinstance(saved.get("team"), str)
                        and isinstance(saved.get("hero"), str)):
                    self.roster_match_id = saved["match_id"]
                    self.roster_team = saved["team"]
                    self.roster_hero = saved["hero"]
                    self.roster_last_clock = number(saved.get("clock"), -1)
                    for side, target in (("allies", self.roster_allies), ("enemies", self.roster_enemies)):
                        names = saved.get(side, [])
                        for name in names[:5] if isinstance(names, list) else []:
                            if isinstance(name, str) and name.startswith("npc_dota_hero_"):
                                target[name] = 0
                    history = saved.get("gpm_history", [])
                    for point in history if isinstance(history, list) else []:
                        if (isinstance(point, dict) and type(point.get("minute")) is int
                                and type(point.get("gpm")) is int
                                and 0 <= point["minute"] <= 360 and 0 <= point["gpm"] <= 10000):
                            self.gpm_history[point["minute"]] = point["gpm"]
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        self.enemy_first_seen: dict[str, float] = {}
        self.enemy_last_seen: dict[str, float] = {}
        self.enemy_last_position: dict[str, dict] = {}
        self.enemy_alerted_missing: set[str] = set()
        self.enemy_missing_alert: dict | None = None
        self.enemy_alert_at = 0.0
        self.enemy_alert_seq = 0
        self.last_advice = 0.0
        self.message = "等待 Dota 2 官方 GSI 数据…"
        self.reason = "waiting"
        self.advice_seq = 0
        self.pending_reason: str | None = None
        self.lane_samples: Counter[str] = Counter()
        self.last_lane_sample_clock = -100
        self.starting_lane = "unknown"
        self.lane_override: str | None = None
        self.current_region = "unknown"
        self.ai_pending = False
        self.paused = False
        self.questions: list[dict] = []
        self.question_pending = False
        self.question_seq = 0
        self.voice_queue: queue.Queue[str] = queue.Queue(maxsize=1)
        self.ai_queue: queue.Queue[tuple[int, State, str, str, str, str, dict]] = queue.Queue(maxsize=1)
        if voice:
            threading.Thread(target=self._voice_worker, daemon=True).start()
        threading.Thread(target=self._ai_worker, daemon=True).start()

    def _save_roster(self) -> None:
        if self.memory_path is None or not self.roster_match_id:
            return
        data = {"match_id": self.roster_match_id, "team": self.roster_team,
                "hero": self.roster_hero, "clock": self.roster_last_clock,
                "allies": list(self.roster_allies), "enemies": list(self.roster_enemies),
                "gpm_history": [{"minute": minute, "gpm": gpm}
                                for minute, gpm in sorted(self.gpm_history.items())]}
        temporary = self.memory_path.with_suffix(self.memory_path.suffix + ".tmp")
        try:
            temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, self.memory_path)
        except OSError as exc:
            print(f"保存本局阵容失败：{type(exc).__name__}", file=sys.stderr)

    def _save_manual_context(self) -> None:
        if self.manual_path is None:
            return
        data = {"status": self.manual_game_status, "note": self.manual_context_note,
                "ended_match_id": self.manual_end_match_id,
                "ended_clock": self.manual_end_clock,
                "new_match_id": self.manual_new_match_id,
                "new_clock": self.manual_new_clock}
        temporary = self.manual_path.with_suffix(self.manual_path.suffix + ".tmp")
        try:
            temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, self.manual_path)
        except OSError as exc:
            print(f"保存手动局势失败：{type(exc).__name__}", file=sys.stderr)

    def set_manual_context(self, action: str, note: str = "") -> None:
        with self.lock:
            if action == "new_match":
                prior_match_id = self.latest.match_id if self.latest else self.roster_match_id
                prior_clock = self.latest.clock if self.latest else self.roster_last_clock
                self._reset_match()
                self.manual_game_status = "new_match"
                self.manual_new_match_id = prior_match_id
                self.manual_new_clock = prior_clock if prior_clock >= 0 else None
                self.message = "你已标记新一局开始；等待本局 GSI 数据…"
                self.reason = "player_new_match"
                self.message_source = "waiting"
            elif action == "ended":
                self.manual_game_status = "ended"
                self.manual_end_match_id = self.latest.match_id if self.latest else self.roster_match_id
                self.manual_end_clock = self.latest.clock if self.latest else self.roster_last_clock
                self.manual_seen_new_pregame = False
                self.advice_seq += 1
                self.ai_pending = False
                self.pending_reason = None
                self.message = "你已标记本局结束；停止自动模型建议。"
                self.reason = "player_ended_match"
                self.message_source = "waiting"
            elif action == "set_note":
                self.manual_context_note = note.strip()
                if self.manual_game_status != "ended" and not self.paused:
                    self.last_advice = 0.0
                    self.advice_seq += 1
                    self.ai_pending = False
                    self.message = "已记录你的补充；等待下一包 GSI 数据后更新建议…"
                    self.message_source = "waiting"
            else:
                raise ValueError("Invalid manual context action")
            self._save_manual_context()

    def _match_roster(self) -> dict:
        allies = ([self.roster_hero] if self.roster_hero else []) + list(self.roster_allies)
        return {"allies": [{"hero": name, "hero_zh": self.localizer.hero(name)}
                            for name in allies[:5]],
                "enemies": [{"hero": name, "hero_zh": self.localizer.hero(name)}
                            for name in list(self.roster_enemies)[:5]],
                "allies_unknown": max(0, 5 - len(allies)),
                "enemies_unknown": max(0, 5 - len(self.roster_enemies)),
                "source": "observed_official_gsi"}

    def _visible_enemies_for_model(self) -> list[dict] | None:
        if self.enemies is None:
            return None
        return [{"hero_schinese": self.localizer.hero(enemy["hero"]),
                 "region_inferred": enemy["region_inferred"]}
                for enemy in self.enemies]

    def _reset_match(self) -> None:
        """Clear match-scoped facts while retaining model settings and pause state."""
        self.previous = None
        self.latest = None
        self.last_received = 0.0
        self.allies = None
        self.enemies = None
        self.last_advice = 0.0
        self.pending_reason = None
        self.advice_seq += 1
        self.ai_pending = False
        self.lane_samples.clear()
        self.last_lane_sample_clock = -100
        self.starting_lane = "unknown"
        self.lane_override = None
        self.role = None
        self.role_group = None
        self.current_region = "unknown"
        self.last_model_state = None
        self.last_model_message = None
        self.enemy_first_seen.clear()
        self.enemy_last_seen.clear()
        self.enemy_last_position.clear()
        self.enemy_alerted_missing.clear()
        self.enemy_missing_alert = None
        self.roster_allies.clear()
        self.roster_enemies.clear()
        self.gpm_history.clear()
        self.roster_match_id = ""
        self.roster_team = ""
        self.roster_hero = ""
        self.roster_last_clock = -1
        self.manual_game_status = None
        self.manual_context_note = ""
        self.manual_end_match_id = ""
        self.manual_end_clock = None
        self.manual_seen_new_pregame = False
        self.manual_new_match_id = ""
        self.manual_new_clock = None
        self.manual_seen_new_selection = False
        self._save_manual_context()
        self.questions.clear()
        self.question_pending = False
        self.message = ("教练已暂停；等待新对局比赛数据。" if self.paused
                        else "等待新对局比赛数据…")
        self.reason = "paused" if self.paused else "waiting"
        self.message_source = "paused" if self.paused else "waiting"
        if self.memory_path is not None:
            try:
                self.memory_path.unlink(missing_ok=True)
            except OSError as exc:
                print(f"清理旧局阵容失败：{type(exc).__name__}", file=sys.stderr)

    def update(self, payload: dict, now: float | None = None) -> str:
        now = time.monotonic() if now is None else now
        with self.lock:
            self.last_packet = now
            game_map = obj(payload.get("map"))
            previous_game_state = self.gsi_game_state
            self.gsi_game_state = str(game_map.get("game_state") or "")
            self.gsi_clock = number(game_map["clock_time"]) if "clock_time" in game_map else None
            for section in ("provider", "map", "player", "hero", "abilities", "items", "minimap"):
                values = obj(payload.get(section))
                if values:
                    self.gsi_schema[section] = sorted(values.keys())
                    if section in ("abilities", "items", "minimap"):
                        child = next((obj(value) for value in values.values() if obj(value)), {})
                        if child:
                            self.gsi_nested_schema[section] = sorted(child.keys())
                    if section == "minimap":
                        allowed = {"unitname", "name", "team", "xpos", "ypos", "image", "type", "visible", "icon"}
                        examples = []
                        details = [obj(entry) for entry in values.values()]
                        details.sort(key=lambda detail: not str(detail.get("unitname", "")).startswith("npc_dota_hero_"))
                        for detail in details:
                            if detail:
                                examples.append({key: value for key, value in detail.items() if key in allowed})
                            if len(examples) >= 20:
                                break
                        self.minimap_diagnostic = {"count": len(values), "examples": examples}
        state = parse_state(payload)
        if state is None:
            if self.gsi_game_state and self.gsi_game_state != GAME_STATE:
                with self.lock:
                    if self.manual_game_status == "ended":
                        if self.gsi_game_state in (
                            "DOTA_GAMERULES_STATE_PRE_GAME",
                            "DOTA_GAMERULES_STATE_HERO_SELECTION",
                            "DOTA_GAMERULES_STATE_STRATEGY_TIME",
                            "DOTA_GAMERULES_STATE_WAIT_FOR_PLAYERS_TO_LOAD",
                        ):
                            self.manual_seen_new_pregame = True
                    elif self.manual_game_status == "new_match":
                        if self.gsi_game_state in (
                            "DOTA_GAMERULES_STATE_PRE_GAME",
                            "DOTA_GAMERULES_STATE_HERO_SELECTION",
                            "DOTA_GAMERULES_STATE_STRATEGY_TIME",
                            "DOTA_GAMERULES_STATE_WAIT_FOR_PLAYERS_TO_LOAD",
                        ):
                            self.manual_seen_new_selection = True
                    elif self.latest is not None or self.roster_match_id:
                        self._reset_match()
            return "ignored"
        with self.lock:
            if self.manual_game_status == "ended":
                new_after_end = (
                    self.manual_seen_new_pregame
                    or (not self.manual_end_match_id and
                        (self.manual_end_clock is None or self.manual_end_clock < 0))
                    or bool(self.manual_end_match_id and state.match_id
                            and state.match_id != self.manual_end_match_id)
                    or bool(self.manual_end_clock is not None
                            and state.clock + 60 < self.manual_end_clock)
                )
                if not new_after_end:
                    return "manual_ended"
                self._reset_match()
            elif self.manual_game_status == "new_match":
                new_after_mark = (
                    self.manual_seen_new_selection
                    or (not self.manual_new_match_id and self.manual_new_clock is None)
                    or bool(self.manual_new_match_id and state.match_id
                            and state.match_id != self.manual_new_match_id)
                    or bool(self.manual_new_clock is not None
                            and state.clock + 60 < self.manual_new_clock)
                )
                if not new_after_mark:
                    return "manual_waiting_new_match"
                self.manual_game_status = None
                self.manual_new_match_id = ""
                self.manual_new_clock = None
                self.manual_seen_new_selection = False
                self._save_manual_context()
            previous = self.previous
            self.allies = visible_allies(payload, state) if isinstance(payload.get("minimap"), dict) else None
            self.enemies = visible_enemies(payload, state) if isinstance(payload.get("minimap"), dict) else None
            roster_conflict = bool(
                (len(self.roster_allies) == 4 and self.allies and
                 any(ally["hero"] not in self.roster_allies for ally in self.allies))
                or (len(self.roster_enemies) == 5 and self.enemies and
                    any(enemy["hero"] not in self.roster_enemies for enemy in self.enemies)))
            new_match = previous is not None and (
                (state.match_id and previous.match_id and state.match_id != previous.match_id)
                or state.clock + 60 < previous.clock
                or (previous.hero and state.hero and previous.hero != state.hero)
                or (previous.team and state.team and previous.team != state.team)
            )
            new_match = new_match or bool(previous is not None and previous_game_state
                                         and previous_game_state != GAME_STATE)
            new_match = new_match or bool(
                (self.roster_match_id and state.match_id != self.roster_match_id)
                or (self.roster_team and state.team and state.team != self.roster_team)
                or (self.roster_hero and state.hero and state.hero != self.roster_hero)
                or (self.roster_last_clock >= 0 and state.clock + 60 < self.roster_last_clock)
                or roster_conflict)
            if new_match:
                previous = None
                self._reset_match()
                self.allies = visible_allies(payload, state) if isinstance(payload.get("minimap"), dict) else None
                self.enemies = visible_enemies(payload, state) if isinstance(payload.get("minimap"), dict) else None
            roster_changed = new_match
            if state.match_id and self.roster_match_id != state.match_id:
                self.roster_match_id = state.match_id
                roster_changed = True
            if state.team and self.roster_team != state.team:
                self.roster_team = state.team
                roster_changed = True
            if state.hero and self.roster_hero != state.hero:
                self.roster_hero = state.hero
                roster_changed = True
            if self.allies is not None:
                for ally in self.allies:
                    name = ally["hero"]
                    if name not in self.roster_allies and len(self.roster_allies) < 4:
                        self.roster_allies[name] = state.clock
                        roster_changed = True
            if self.enemies is not None:
                for enemy in self.enemies:
                    name = enemy["hero"]
                    if name not in self.roster_enemies and len(self.roster_enemies) < 5:
                        self.roster_enemies[name] = state.clock
                        roster_changed = True
            self.roster_last_clock = state.clock
            minute = state.clock // 60
            if state.gpm is not None and 0 <= state.gpm <= 10000 and minute <= 360:
                if minute not in self.gpm_history:
                    roster_changed = True
                self.gpm_history[minute] = state.gpm
            if roster_changed:
                self._save_roster()
            if self.enemies is not None:
                visible_now = {enemy["hero"] for enemy in self.enemies}
                for enemy in self.enemies:
                    name = enemy["hero"]
                    if self.enemy_missing_alert and self.enemy_missing_alert["hero"] == name:
                        self.enemy_missing_alert = None
                    if name not in self.enemy_last_seen or now - self.enemy_last_seen[name] > 2:
                        self.enemy_first_seen[name] = now
                        self.enemy_alerted_missing.discard(name)
                    self.enemy_last_seen[name] = now
                    self.enemy_last_position[name] = enemy
                for name, last_seen in self.enemy_last_seen.items():
                    if (name not in visible_now and name not in self.enemy_alerted_missing and
                            last_seen - self.enemy_first_seen.get(name, last_seen) >= 3 and
                            now - last_seen >= 4):
                        self.enemy_alerted_missing.add(name)
                        self.enemy_missing_alert = self.enemy_last_position[name]
                        self.enemy_alert_at = now
                        self.enemy_alert_seq += 1
            self.current_region = map_region(state.xpos, state.ypos)
            if (0 <= state.clock <= 7 * 60 and state.clock - self.last_lane_sample_clock >= 5
                    and self.current_region in ("top", "mid", "bottom")):
                self.lane_samples[self.current_region] += 1
                self.last_lane_sample_clock = state.clock
                region, count = self.lane_samples.most_common(1)[0]
                total = sum(self.lane_samples.values())
                if total >= 6 and count / total >= 0.7:
                    self.starting_lane = lane_type(region, state.team)
            if self.paused:
                self.previous = state
                self.latest = state
                self.last_received = now
                self.pending_reason = None
                return "paused"
            reason = None
            if previous is not None:
                if previous.alive is True and state.alive is False:
                    reason = "death"
                elif previous.alive is False and state.alive is True:
                    reason = "respawn"
                elif (previous.hp_percent is not None and previous.hp_percent >= 35
                      and state.hp_percent is not None and state.hp_percent < 35):
                    reason = "low_health"
                elif (previous.mana_percent is not None and previous.mana_percent >= 25
                      and state.mana_percent is not None and state.mana_percent < 25):
                    reason = "low_mana"
                elif previous.ultimate == "cooling" and state.ultimate == "ready":
                    reason = "ultimate_ready"
                elif state.level in (6, 12, 18) and previous.level < state.level:
                    reason = "level"
                elif (MAJOR_ITEMS & set(state.items)) - set(previous.items):
                    reason = "major_item"
            if reason and reason != "death" and self.last_advice and now - self.last_advice < MIN_GAP_SECONDS:
                self.pending_reason = reason
                reason = None
            if self.pending_reason == "low_health" and (state.hp_percent is None or state.hp_percent >= 35):
                self.pending_reason = None
            if self.pending_reason == "low_mana" and (state.mana_percent is None or state.mana_percent >= 25):
                self.pending_reason = None
            if reason is None and self.pending_reason and now - self.last_advice >= MIN_GAP_SECONDS:
                reason, self.pending_reason = self.pending_reason, None
            if reason is None and state.alive is not False and (not self.last_advice or now - self.last_advice >= self.interval):
                reason = "regular"
            self.previous = state
            self.latest = state
            self.last_received = now
            if reason and (reason == "death" or new_match or not self.last_advice or now - self.last_advice >= MIN_GAP_SECONDS):
                model_ready = bool(self.ai_model and self.api_key)
                self.message = "正在生成建议…" if model_ready else "已收到比赛数据；启用模型后生成建议。"
                self.message_source = "pending" if model_ready else "off"
                self.reason = reason
                self.advice_seq += 1
                self.last_advice = now
                if model_ready:
                    self.ai_pending = True
                    self.ai_error = None
                    prior = self.last_model_state
                    delta = None
                    if prior and state.clock >= prior.clock:
                        delta = {"seconds": state.clock - prior.clock,
                                 "last_hits": state.last_hits - prior.last_hits if state.last_hits is not None and prior.last_hits is not None else None,
                                 "deaths": state.deaths - prior.deaths,
                                 "gold_change": state.gold - prior.gold if state.gold is not None and prior.gold is not None else None}
                    context = {"current_region_inferred": self.current_region,
                               "starting_lane_inferred": self.starting_lane,
                               "lane_supplied_by_player": self.lane_override,
                               "match_roster_from_observed_gsi": self._match_roster(),
                               "player_reported_context": self.manual_context_note or None,
                               "visible_enemies_from_gsi_minimap": self._visible_enemies_for_model(),
                               "teammates_from_gsi_minimap": [
                                   {**ally, "hero_schinese": self.localizer.hero(ally["hero"])}
                                   for ally in self.allies] if self.allies is not None else None,
                               "recent_gsi_change": delta,
                               "previous_model_advice": self.last_model_message}
                    self.last_model_state = state
                    try:
                        self.ai_queue.put_nowait((self.advice_seq, state, reason,
                                                  self.ai_provider, self.ai_model, self.api_key, context))
                    except queue.Full:
                        try:
                            self.ai_queue.get_nowait()
                        except queue.Empty:
                            pass
                        self.ai_queue.put_nowait((self.advice_seq, state, reason,
                                                  self.ai_provider, self.ai_model, self.api_key, context))
                return "advised"
            return "received"

    def status(self) -> dict:
        with self.lock:
            lane = self.lane_override or self.starting_lane
            effective_role = self.role if self.role is not None else infer_role(lane, self.role_group)
            return {
                "connected": self.last_packet > 0 and time.monotonic() - self.last_packet < 15,
                "in_progress": (self.manual_game_status != "ended" and self.gsi_game_state == GAME_STATE
                                and self.latest is not None
                                and time.monotonic() - self.last_received < 15),
                "manual_game_status": self.manual_game_status,
                "manual_context_note": self.manual_context_note,
                "gsi_game_state": self.gsi_game_state,
                "gsi_clock": self.gsi_clock,
                "state": asdict(self.latest) if self.latest else None,
                "gpm_history": [{"minute": minute, "gpm": gpm}
                                for minute, gpm in sorted(self.gpm_history.items())],
                "hero_zh": self.localizer.hero(self.latest.hero) if self.latest else None,
                "items_zh": [self.localizer.item(item) for item in self.latest.items] if self.latest else [],
                "teammates": [{**ally, "hero_zh": self.localizer.hero(ally["hero"])}
                              for ally in self.allies] if self.allies is not None else None,
                "visible_enemies": [{**enemy, "hero_zh": self.localizer.hero(enemy["hero"])}
                                    for enemy in self.enemies] if self.enemies is not None else None,
                "match_roster": self._match_roster(),
                "enemy_missing_alert": ({**self.enemy_missing_alert,
                                         "hero_zh": self.localizer.hero(self.enemy_missing_alert["hero"])}
                                        if self.enemy_missing_alert and time.monotonic() - self.enemy_alert_at < 20
                                        else None),
                "enemy_alert_seq": self.enemy_alert_seq,
                "localization_source": self.localizer.source,
                "message": self.message,
                "reason": self.reason,
                "advice_seq": self.advice_seq,
                "role": self.role,
                "role_group": self.role_group,
                "effective_role": effective_role,
                "role_source": "manual" if self.role is not None else
                               "lane_group_inference" if effective_role is not None else "unknown",
                "current_region": self.current_region,
                "starting_lane": self.starting_lane,
                "lane_override": self.lane_override,
                "effective_lane": lane,
                "lane_samples": sum(self.lane_samples.values()),
                "voice": self.voice,
                "ai_model": self.ai_model,
                "ai_provider": self.ai_provider if self.ai_model and self.api_key else None,
                "ai_ready": bool(self.ai_model and self.api_key),
                "ai_error": self.ai_error,
                "last_response_model": self.last_response_model,
                "last_token_usage": self.last_token_usage,
                "session_token_usage": dict(self.session_token_usage),
                "usage_reported_calls": self.usage_reported_calls,
                "message_source": self.message_source,
                "ai_pending": self.ai_pending,
                "coach_paused": self.paused,
                "questions": [dict(entry) for entry in self.questions],
                "question_pending": self.question_pending,
                "interval": self.interval,
            }

    def ask(self, question: str) -> tuple[int, str]:
        question = question.strip()
        if not question or len(question) > 500:
            return 400, "问题须为 1 到 500 个字符"
        with self.lock:
            if self.paused:
                return 409, "教练已暂停，请先恢复教练"
            if not self.ai_model or not self.api_key:
                return 409, "请先配置模型"
            if self.question_pending:
                return 409, "上一条问题仍在回答中"
            self.question_seq += 1
            sequence = self.question_seq
            self.question_pending = True
            self.questions.append({"id": sequence, "question": question,
                                   "answer": None, "status": "pending"})
            self.questions = self.questions[-10:]
            state = (self.latest if self.manual_game_status != "ended" and self.last_received
                     and time.monotonic() - self.last_received < 15 else None)
            lane = self.lane_override or self.starting_lane
            role = self.role if self.role is not None else infer_role(lane, self.role_group)
            group = self.role_group
            source = "manual" if self.role is not None else "lane_group_inference" if role else "unknown"
            context = {"current_region_inferred": self.current_region if state else "unknown",
                       "starting_lane_inferred": self.starting_lane if state else "unknown",
                       "lane_supplied_by_player": self.lane_override,
                       "player_reported_match_status": self.manual_game_status,
                       "player_reported_context": self.manual_context_note or None,
                       "match_roster_from_observed_gsi": self._match_roster(),
                       "visible_enemies_from_gsi_minimap": self._visible_enemies_for_model() if state else None,
                       "teammates_from_gsi_minimap": [
                           {**ally, "hero_schinese": self.localizer.hero(ally["hero"])}
                           for ally in self.allies] if state and self.allies is not None else None}
            provider, model, key = self.ai_provider, self.ai_model, self.api_key
        threading.Thread(target=self._question_worker,
                         args=(sequence, state, role, group, source, context, provider, model, key, question),
                         daemon=True).start()
        return 202, "已提交"

    def _question_worker(self, sequence: int, state: State | None, role: int | None,
                         group: str | None, source: str, context: dict,
                         provider: str, model: str, key: str, question: str) -> None:
        try:
            result = request_ai_advice(state, "player_question", role, model, key,
                                       self.localizer, group, source, provider, context, question)
            error = result.error
        except Exception as exc:
            print(f"AI 提问失败：{type(exc).__name__}", file=sys.stderr)
            error = (f"HTTP {exc.code}" if isinstance(exc, HTTPError) else
                     "网络连接失败" if isinstance(exc, URLError) else "响应格式异常")
            result = None
        with self.lock:
            if result is not None:
                usage = {"input_tokens": result.input_tokens,
                         "output_tokens": result.output_tokens,
                         "total_tokens": result.total_tokens}
                self.last_response_model = result.model
                self.last_token_usage = usage
                if any(value is not None for value in usage.values()):
                    self.usage_reported_calls += 1
                    for name, value in usage.items():
                        if value is not None:
                            self.session_token_usage[name] += value
            entry = next((item for item in self.questions if item["id"] == sequence), None)
            if entry is None or entry["status"] != "pending" or self.paused:
                return
            self.question_pending = False
            entry["status"] = "done" if result and result.message else "error"
            entry["answer"] = result.message if result and result.message else f"提问失败：{error}"

    def set_paused(self, paused: bool) -> None:
        with self.lock:
            if self.paused == paused:
                return
            self.paused = paused
            self.advice_seq += 1
            self.ai_pending = False
            self.pending_reason = None
            self.last_advice = 0.0
            if paused:
                self.message = "教练已暂停；继续在本机接收 GSI 数据，不生成新建议。"
                self.reason = "paused"
                self.message_source = "paused"
                for entry in self.questions:
                    if entry["status"] == "pending":
                        entry["status"] = "cancelled"
                        entry["answer"] = "教练已暂停，本次回答不再显示。"
                for message_queue in (self.ai_queue, self.voice_queue):
                    try:
                        message_queue.get_nowait()
                    except queue.Empty:
                        pass
            else:
                self.message = "教练已恢复，等待下一包 GSI 数据…"
                self.reason = "waiting"
                self.message_source = "waiting"

    def schema(self) -> dict:
        with self.lock:
            return {
                "subscribed_sections": ["provider", "map", "player", "hero", "abilities", "items", "minimap"],
                "observed_fields": dict(self.gsi_schema),
                "nested_example_fields": dict(self.gsi_nested_schema),
                "minimap_diagnostic": dict(self.minimap_diagnostic),
                "note": "Field names only; no player identifiers or raw GSI values are exposed here.",
            }

    def _ai_worker(self) -> None:
        while True:
            sequence, state, reason, provider, model, api_key, context = self.ai_queue.get()
            try:
                with self.lock:
                    if self.paused or sequence != self.advice_seq:
                        continue
                    lane = self.lane_override or self.starting_lane
                    role = self.role if self.role is not None else infer_role(lane, self.role_group)
                    group = self.role_group
                    source = "manual" if self.role is not None else "lane_group_inference" if role else "unknown"
                result = request_ai_advice(state, reason, role, model, api_key,
                                           self.localizer, group, source, provider, context)
                message = result.message
                error = result.error
            except Exception as exc:
                detail = str(exc) if isinstance(exc, ValueError) else ""
                print(f"AI 请求失败：{type(exc).__name__} {detail}", file=sys.stderr)
                error = (f"HTTP {exc.code}" if isinstance(exc, HTTPError) else
                         "网络连接失败" if isinstance(exc, URLError) else
                         detail if isinstance(exc, ValueError) else "响应格式异常")
                message = None
                result = None
            with self.lock:
                if result is not None:
                    usage = {"input_tokens": result.input_tokens,
                             "output_tokens": result.output_tokens,
                             "total_tokens": result.total_tokens}
                    self.last_response_model = result.model
                    self.last_token_usage = usage
                    if any(value is not None for value in usage.values()):
                        self.usage_reported_calls += 1
                        for key, value in usage.items():
                            if value is not None:
                                self.session_token_usage[key] += value
                if sequence != self.advice_seq:
                    continue
                self.ai_pending = False
                if message:
                    self.message = message
                    self.last_model_message = message
                    self.message_source = "model"
                    self.ai_error = None
                    self.reason = "ai_" + reason
                    print(f"[AI 建议] {message}", flush=True)
                else:
                    self.message = f"模型未生成建议（{error or '未知原因'}）。"
                    self.message_source = "error"
                    self.ai_error = error
                if message and self.voice:
                    try:
                        self.voice_queue.put_nowait(self.message)
                    except queue.Full:
                        pass

    def _voice_worker(self) -> None:
        if sys.platform != "win32":
            print("语音仅支持 Windows；文字提示继续可用。", file=sys.stderr)
            return
        script = (
            "$ErrorActionPreference = 'Stop'; Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "$zh = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -eq 'zh-CN' } | Select-Object -First 1; "
            "if ($zh) { $s.SelectVoice($zh.VoiceInfo.Name) }; "
            "$s.Rate = 2; "
            "$input | ForEach-Object { $s.Speak($_) }"
        )
        while True:
            message = self.voice_queue.get()
            try:
                result = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                    input=message, text=True, capture_output=True, timeout=25,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if result.returncode:
                    print("语音播报失败：" + result.stderr.strip()[:300], file=sys.stderr)
                    print("文字提示继续可用；也可在状态页启用网页语音。", file=sys.stderr)
                    return
            except (OSError, subprocess.TimeoutExpired) as exc:
                print(f"语音播报失败：{exc}", file=sys.stderr)
                return


def make_handler(coach: Coach):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _send(self, status: int, data: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path == "/status":
                data = json.dumps(coach.status(), ensure_ascii=False).encode("utf-8")
                self._send(200, data, "application/json; charset=utf-8")
            elif self.path == "/schema":
                data = json.dumps(coach.schema(), ensure_ascii=False).encode("utf-8")
                self._send(200, data, "application/json; charset=utf-8")
            elif self.path == "/":
                data = (Path(__file__).with_name("dashboard.html")).read_bytes()
                self._send(200, data, "text/html; charset=utf-8")
            elif self.path == "/marked.min.js":
                data = (Path(__file__).with_name("marked.min.js")).read_bytes()
                self._send(200, data, "text/javascript; charset=utf-8")
            else:
                self._send(404, b"Not found", "text/plain")

        def do_POST(self) -> None:
            if self.path not in ("/gsi", "/settings", "/ai-settings", "/coach-control",
                                 "/match-context", "/ask"):
                self._send(404, b"Not found", "text/plain")
                return
            length = number(self.headers.get("Content-Length"), -1)
            if length < 0 or length > MAX_BODY_BYTES:
                self._send(413, b"Invalid body size", "text/plain")
                return
            try:
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("Expected JSON object")
            except (ValueError, UnicodeError):
                self._send(400, b"Invalid JSON", "text/plain")
                return
            if self.path == "/ask":
                question = payload.get("question")
                if set(payload) != {"question"} or not isinstance(question, str):
                    self._send(400, b"Invalid question", "text/plain")
                    return
                status, message = coach.ask(question)
                self._send(status, json.dumps({"message": message}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
                return
            if self.path == "/match-context":
                action = payload.get("action")
                note = payload.get("note", "")
                if (action not in ("new_match", "ended", "set_note")
                        or set(payload) - {"action", "note"}
                        or not isinstance(note, str) or len(note) > 300
                        or (action == "set_note" and "note" not in payload)):
                    self._send(400, b"Invalid match context", "text/plain")
                    return
                coach.set_manual_context(action, note)
                self._send(200, b'{"status":"ok"}', "application/json")
                return
            if self.path == "/coach-control":
                if set(payload) != {"paused"} or not isinstance(payload["paused"], bool):
                    self._send(400, b"Invalid coach control", "text/plain")
                    return
                coach.set_paused(payload["paused"])
                self._send(200, b'{"status":"ok"}', "application/json")
                return
            if self.path == "/ai-settings":
                provider = payload.get("provider")
                model = payload.get("model")
                supplied_key = payload.get("api_key", "")
                if provider not in ("off", "openrouter", "deepseek", "openai") or not isinstance(supplied_key, str):
                    self._send(400, b"Invalid AI settings", "text/plain")
                    return
                if len(supplied_key) > 500 or (model is not None and
                    (not isinstance(model, str) or not model.strip() or len(model) > 100)):
                    self._send(400, b"Invalid AI settings", "text/plain")
                    return
                with coach.lock:
                    if provider == "off":
                        coach.ai_model = None
                        coach.api_key = ""
                    else:
                        key = (supplied_key.strip() or
                               (coach.api_key if coach.ai_provider == provider else "") or
                               os.environ.get({"openrouter": "OPENROUTER_API_KEY",
                                               "deepseek": "DEEPSEEK_API_KEY",
                                               "openai": "OPENAI_API_KEY"}[provider], ""))
                        if not key:
                            self._send(400, b"API key required", "text/plain")
                            return
                        coach.ai_provider = provider
                        coach.ai_model = model.strip() if model is not None else AI_DEFAULT_MODELS[provider]
                        coach.api_key = key
                    coach.ai_error = None
                    coach.ai_pending = False
                    coach.advice_seq += 1
                    if not coach.paused:
                        coach.message = ("等待下一包比赛数据后生成建议…" if coach.ai_model and coach.api_key
                                         else "已收到比赛数据；启用模型后生成建议。")
                        coach.message_source = "waiting" if coach.ai_model and coach.api_key else "off"
                        coach.reason = "settings"
                    if coach.latest is not None:
                        coach.last_advice = 0.0
                self._send(200, b'{"status":"ok"}', "application/json")
                return
            if self.path == "/settings":
                if "role_group" in payload:
                    role_group = payload["role_group"]
                    if role_group is not None and role_group not in ("core", "support"):
                        self._send(400, b"Invalid role group", "text/plain")
                        return
                if "role" in payload:
                    role = payload["role"]
                    if role is not None and (not isinstance(role, int) or isinstance(role, bool) or role not in range(1, 6)):
                        self._send(400, b"Invalid role", "text/plain")
                        return
                if "lane" in payload:
                    lane = payload["lane"]
                    if lane is not None and lane not in ("safelane", "mid", "offlane"):
                        self._send(400, b"Invalid lane", "text/plain")
                        return
                with coach.lock:
                    if "role_group" in payload:
                        coach.role_group = role_group
                        if "role" not in payload:
                            coach.role = None
                    if "role" in payload:
                        coach.role = role
                    if "lane" in payload:
                        coach.lane_override = lane
                    if (coach.latest is not None and not coach.paused and
                            any(key in payload for key in ("role", "role_group", "lane"))):
                        coach.message = ("等待下一包比赛数据后生成建议…" if coach.ai_model and coach.api_key
                                         else "已收到比赛数据；启用模型后生成建议。")
                        coach.message_source = "waiting" if coach.ai_model and coach.api_key else "off"
                        coach.reason = "settings"
                        coach.advice_seq += 1
                        coach.ai_pending = False
                        coach.last_advice = 0.0
                self._send(200, b'{"status":"ok"}', "application/json")
                return
            result = coach.update(payload)
            self._send(200, json.dumps({"status": result}).encode(), "application/json")

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Dota 2 本地新手节奏教练")
    parser.add_argument("--role", type=int, choices=range(1, 6), help="本局位置，1 到 5；可不填")
    parser.add_argument("--no-voice", action="store_true", help="只显示文字")
    parser.add_argument("--start-paused", action="store_true", help="启动时暂停模型请求，继续本地接收 GSI")
    parser.add_argument("--ai-provider", choices=("auto", "off", "openrouter", "deepseek", "openai"),
                        default="auto", help="模型服务商；auto 优先使用已有环境变量中的密钥")
    parser.add_argument("--ai-model", help="覆盖所选服务商的默认模型 ID")
    parser.add_argument("--interval", type=int, default=REGULAR_GAP_SECONDS,
                        help="常规巡检间隔秒数（30 到 120，默认 45）")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    provider_keys = {"openrouter": os.environ.get("OPENROUTER_API_KEY", ""),
                     "deepseek": os.environ.get("DEEPSEEK_API_KEY", ""),
                     "openai": os.environ.get("OPENAI_API_KEY", "")}
    provider = args.ai_provider
    if provider == "auto":
        provider = next((name for name in ("openrouter", "deepseek", "openai") if provider_keys[name]), "off")
    api_key = provider_keys.get(provider, "")
    if provider != "off" and not api_key:
        parser.error(f"{provider} 需要相应的 API Key 环境变量，或启动后在页面输入")
    if args.ai_model and provider == "off":
        parser.error("--ai-model 需要同时启用模型服务商")
    if not 30 <= args.interval <= 120:
        parser.error("--interval 必须在 30 到 120 秒之间")
    localizer = Localizer.from_game()
    print(f"简中游戏文本：{len(localizer.tokens)} 条", flush=True)
    model = (args.ai_model or AI_DEFAULT_MODELS[provider]) if provider != "off" else None
    coach = Coach(args.role, not args.no_voice, model, api_key, args.interval, localizer,
                  provider if provider != "off" else "openrouter",
                  memory_path=Path(__file__).with_name("match_memory.json"),
                  manual_path=Path(__file__).with_name("manual_context.json"))
    if args.start_paused:
        coach.set_paused(True)
    print(f"建议模式：{provider + ' / ' + model if model else '未启用模型'}", flush=True)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(coach))
    print(f"教练已启动：http://127.0.0.1:{args.port}/  GSI: http://127.0.0.1:{args.port}/gsi", flush=True)
    print("Ctrl+F11 快捷键由交互桌面的 hotkey_helper.py 负责。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
