import unittest
import io
import json
import http.client
import threading
import time
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer

from coach import Coach, infer_role, lane_type, make_handler, map_region, parse_state, request_ai_advice, visible_allies, visible_enemies
from localization import Localizer


def packet(clock=60, alive=True, level=5, cooldown=0, item="item_boots"):
    return {
        "map": {"game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS", "clock_time": clock, "matchid": 123},
        "player": {"kills": 1, "deaths": 0, "assists": 2, "gold": 500, "gpm": 380},
        "hero": {"name": "npc_dota_hero_axe", "level": level, "alive": alive,
                 "health": 800, "max_health": 1000, "mana_percent": 70},
        "abilities": {"ability3": {"ultimate": True, "level": 1, "cooldown": cooldown}},
        "items": {"slot0": {"name": item}},
    }


class CoachTests(unittest.TestCase):
    def test_gpm_history_samples_once_per_minute_and_resets_with_match(self):
        coach = Coach(role=None, voice=False)
        coach.set_paused(True)
        for clock, gpm in ((60, 380), (89, 395), (120, 370), (241, 420)):
            data = packet(clock=clock)
            data["player"]["gpm"] = gpm
            coach.update(data, now=time.monotonic())
        self.assertEqual(coach.status()["gpm_history"], [
            {"minute": 1, "gpm": 395}, {"minute": 2, "gpm": 370},
            {"minute": 4, "gpm": 420}])
        next_match = packet(clock=10)
        next_match["map"]["matchid"] = 124
        coach.update(next_match, now=time.monotonic())
        self.assertEqual(coach.status()["gpm_history"], [{"minute": 0, "gpm": 380}])

    def test_gpm_history_survives_restart_and_manual_new_match(self):
        memory_path = Path(__file__).with_name(".test_gpm_memory.json")
        try:
            coach = Coach(role=None, voice=False, memory_path=memory_path)
            coach.set_paused(True)
            coach.update(packet(clock=60), now=time.monotonic())
            coach.update(packet(clock=120), now=time.monotonic())
            restored = Coach(role=None, voice=False, memory_path=memory_path)
            self.assertEqual(restored.status()["gpm_history"], [
                {"minute": 1, "gpm": 380}, {"minute": 2, "gpm": 380}])
            restored.set_manual_context("new_match")
            self.assertEqual(restored.status()["gpm_history"], [])
            self.assertFalse(memory_path.exists())
        finally:
            memory_path.unlink(missing_ok=True)
            memory_path.with_suffix(memory_path.suffix + ".tmp").unlink(missing_ok=True)

    def test_manual_match_status_and_note_survive_restart_and_reset(self):
        manual_path = Path(__file__).with_name(".test_manual_context.json")
        memory_path = Path(__file__).with_name(".test_manual_memory.json")
        try:
            coach = Coach(role=None, voice=False, memory_path=memory_path,
                          manual_path=manual_path)
            coach.set_paused(True)
            coach.update(packet(clock=540), now=time.monotonic())
            coach.set_manual_context("set_note", "我打辅助，对面一直抓中。")
            self.assertEqual(coach.status()["manual_context_note"], "我打辅助，对面一直抓中。")
            coach.set_manual_context("ended")
            self.assertFalse(coach.status()["in_progress"])
            self.assertEqual(coach.update(packet(clock=550), now=time.monotonic()), "manual_ended")
            restored = Coach(role=None, voice=False, memory_path=memory_path,
                             manual_path=manual_path)
            self.assertEqual(restored.status()["manual_game_status"], "ended")
            self.assertEqual(restored.status()["manual_context_note"], "我打辅助，对面一直抓中。")
            restored.set_manual_context("new_match")
            status = restored.status()
            self.assertIsNone(status["state"])
            self.assertEqual(status["match_roster"]["enemies_unknown"], 5)
            self.assertEqual(status["manual_context_note"], "")
            self.assertEqual(status["manual_game_status"], "new_match")
            self.assertFalse(memory_path.exists())
            restored.set_paused(True)
            self.assertEqual(restored.update(packet(clock=560), now=time.monotonic()),
                             "manual_waiting_new_match")
            self.assertIsNone(restored.status()["state"])
            self.assertEqual(restored.update(packet(clock=10), now=time.monotonic()), "paused")
            self.assertIsNone(restored.status()["manual_game_status"])
        finally:
            for path in (manual_path, memory_path):
                path.unlink(missing_ok=True)
                path.with_suffix(path.suffix + ".tmp").unlink(missing_ok=True)

    def test_manual_end_resets_when_next_match_pregame_appears(self):
        coach = Coach(role=None, voice=False)
        coach.set_paused(True)
        coach.update(packet(clock=300), now=time.monotonic())
        coach.set_manual_context("ended")
        pregame = packet(clock=-70)
        pregame["map"]["game_state"] = "DOTA_GAMERULES_STATE_PRE_GAME"
        coach.update(pregame, now=time.monotonic())
        self.assertEqual(coach.update(packet(clock=1), now=time.monotonic()), "paused")
        self.assertIsNone(coach.status()["manual_game_status"])

    def test_pregame_clears_previous_match_even_with_same_practice_match_id(self):
        memory_path = Path(__file__).with_name(".test_match_transition.json")
        try:
            old = packet(clock=450)
            old["map"]["matchid"] = 0
            old["player"]["team_name"] = "radiant"
            old["minimap"] = {"enemy": {"unitname": "npc_dota_hero_lion",
                "image": "minimap_enemyicon", "team": 3, "xpos": 100, "ypos": 100}}
            coach = Coach(role=2, voice=False, memory_path=memory_path)
            coach.update(old, now=100)
            self.assertTrue(memory_path.exists())
            pregame = packet(clock=-73)
            pregame["map"]["matchid"] = 0
            pregame["map"]["game_state"] = "DOTA_GAMERULES_STATE_PRE_GAME"
            coach.update(pregame, now=110)
            status = coach.status()
            self.assertIsNone(status["state"])
            self.assertIsNone(status["hero_zh"])
            self.assertIsNone(status["role"])
            self.assertEqual(status["match_roster"]["enemies_unknown"], 5)
            self.assertFalse(memory_path.exists())
            fresh = packet(clock=0)
            fresh["map"]["matchid"] = 0
            fresh["player"]["team_name"] = "radiant"
            fresh["minimap"] = {"enemy": {"unitname": "npc_dota_hero_sniper",
                "image": "minimap_enemyicon", "team": 3, "xpos": 200, "ypos": 200}}
            coach.update(fresh, now=120)
            self.assertEqual([hero["hero"] for hero in coach.status()["match_roster"]["enemies"]],
                             ["npc_dota_hero_sniper"])
        finally:
            memory_path.unlink(missing_ok=True)

    def test_match_roster_remembers_seen_heroes_and_resets_for_next_match(self):
        memory_path = Path(__file__).with_name(".test_match_memory.json")
        temporary = memory_path.with_suffix(memory_path.suffix + ".tmp")
        try:
            data = packet(clock=100)
            data["map"]["matchid"] = 0
            data["player"]["team_name"] = "radiant"
            data["minimap"] = {
                "ally": {"unitname": "npc_dota_hero_lina", "image": "minimap_herocircle",
                         "team": 2, "xpos": 100, "ypos": 200},
                "enemy": {"unitname": "npc_dota_hero_sniper", "image": "minimap_enemyicon",
                          "team": 3, "xpos": 300, "ypos": 400},
            }
            coach = Coach(role=None, voice=False, memory_path=memory_path)
            coach.update(data, now=100)
            data["minimap"] = {}
            coach.update(data, now=105)
            roster = coach.status()["match_roster"]
            self.assertEqual([hero["hero"] for hero in roster["allies"]],
                             ["npc_dota_hero_axe", "npc_dota_hero_lina"])
            self.assertEqual([hero["hero"] for hero in roster["enemies"]],
                             ["npc_dota_hero_sniper"])
            self.assertEqual(roster["enemies_unknown"], 4)
            self.assertNotIn("xpos", memory_path.read_text(encoding="utf-8"))

            restored = Coach(role=None, voice=False, memory_path=memory_path)
            restored.update(data, now=110)
            self.assertEqual(restored.status()["match_roster"]["enemies_unknown"], 4)
            restored.role = 2
            restored.role_group = "core"
            restored.lane_override = "mid"
            next_match = packet(clock=20)
            next_match["map"]["matchid"] = 0
            next_match["player"]["team_name"] = "radiant"
            next_match["minimap"] = {
                "enemy": {"unitname": "npc_dota_hero_lion", "image": "minimap_enemyicon",
                          "team": 3, "xpos": 300, "ypos": 400}}
            restored.update(next_match, now=120)
            self.assertEqual([hero["hero"] for hero in restored.status()["match_roster"]["enemies"]],
                             ["npc_dota_hero_lion"])
            self.assertIsNone(restored.status()["role"])
            self.assertIsNone(restored.status()["role_group"])
            self.assertIsNone(restored.status()["lane_override"])
        finally:
            memory_path.unlink(missing_ok=True)
            temporary.unlink(missing_ok=True)

    def test_full_roster_conflict_resets_practice_match_with_zero_id(self):
        data = packet(clock=100)
        data["map"]["matchid"] = 0
        data["player"]["team_name"] = "radiant"
        names = ["lion", "warlock", "dragon_knight", "zuus", "drow_ranger"]
        data["minimap"] = {str(index): {"unitname": "npc_dota_hero_" + name,
            "image": "minimap_enemyicon", "team": 3, "xpos": index * 100,
            "ypos": index * 100} for index, name in enumerate(names)}
        coach = Coach(role=None, voice=False)
        coach.update(data, now=100)
        self.assertEqual(coach.status()["match_roster"]["enemies_unknown"], 0)
        changed = packet(clock=200)
        changed["map"]["matchid"] = 0
        changed["player"]["team_name"] = "radiant"
        changed["minimap"] = {"enemy": {"unitname": "npc_dota_hero_bane",
            "image": "minimap_enemyicon", "team": 3, "xpos": 300, "ypos": 400}}
        coach.update(changed, now=200)
        self.assertEqual([hero["hero"] for hero in coach.status()["match_roster"]["enemies"]],
                         ["npc_dota_hero_bane"])

    def test_minimap_diagnostic_keeps_only_position_fields(self):
        data = packet()
        data["minimap"] = {
            "entity0": {"unitname": "npc_dota_hero_axe", "team": 2,
                        "xpos": 123, "ypos": 456, "accountid": 98765},
            "entity1": {"unitname": "npc_dota_hero_lina", "team": 2,
                        "xpos": 789, "ypos": 234},
        }
        coach = Coach(role=None, voice=False)
        coach.update(data, now=100)
        diagnostic = coach.schema()["minimap_diagnostic"]
        self.assertEqual(diagnostic["count"], 2)
        self.assertEqual(diagnostic["examples"][1]["unitname"], "npc_dota_hero_lina")
        self.assertNotIn("accountid", diagnostic["examples"][0])

    def test_only_real_allied_hero_icons_are_used(self):
        data = packet()
        data["player"]["team_name"] = "radiant"
        data["hero"].update({"xpos": -3000, "ypos": -3000})
        data["minimap"] = {
            "self": {"unitname": "npc_dota_hero_axe", "image": "minimap_herocircle_self", "team": 2, "xpos": -3000, "ypos": -3000},
            "ally": {"unitname": "npc_dota_hero_lina", "image": "minimap_herocircle", "team": 2, "xpos": -2000, "ypos": -2000},
            "enemy": {"unitname": "npc_dota_hero_sniper", "image": "minimap_herocircle", "team": 3, "xpos": 0, "ypos": 0},
            "circle": {"unitname": "npc_dota_hero_juggernaut", "image": "minimap_plaincircle", "team": 2, "xpos": 0, "ypos": 0},
        }
        self.assertEqual([ally["hero"] for ally in visible_allies(data, parse_state(data))],
                         ["npc_dota_hero_lina"])
        coach = Coach(role=None, voice=False)
        coach.update(data, now=100)
        self.assertEqual(coach.status()["teammates"][0]["hero"], "npc_dota_hero_lina")

    def test_enemy_disappearance_requires_stable_prior_visibility(self):
        data = packet()
        data["player"]["team_name"] = "radiant"
        enemy = {"unitname": "npc_dota_hero_sniper", "image": "minimap_enemyicon",
                 "team": 3, "xpos": 200, "ypos": 300}
        data["minimap"] = {"enemy": enemy,
                           "plain": {**enemy, "unitname": "npc_dota_hero_bane", "image": "minimap_plaincircle"}}
        self.assertEqual([e["hero"] for e in visible_enemies(data, parse_state(data))],
                         ["npc_dota_hero_sniper"])
        coach = Coach(role=None, voice=False)
        coach.update(data, now=100)
        coach.update(data, now=102)
        coach.update(data, now=104)
        data["minimap"] = {}
        coach.update(data, now=107)
        self.assertEqual(coach.enemy_alert_seq, 0)
        coach.update(data, now=108)
        self.assertEqual(coach.enemy_alert_seq, 1)
        self.assertEqual(coach.enemy_missing_alert["hero"], "npc_dota_hero_sniper")
        coach.update(data, now=118)
        self.assertEqual(coach.enemy_alert_seq, 1)
        data["minimap"] = {"enemy": enemy}
        coach.update(data, now=119)
        self.assertIsNone(coach.enemy_missing_alert)

    def test_parse_real_fields_and_unknowns(self):
        state = parse_state(packet())
        self.assertEqual((state.hero, state.hp_percent, state.ultimate, state.items),
                         ("npc_dota_hero_axe", 80, "ready", ("item_boots",)))
        missing = packet()
        missing.pop("abilities")
        missing["hero"].pop("mana_percent")
        self.assertEqual(parse_state(missing).ultimate, "unknown")
        self.assertIsNone(parse_state(missing).mana_percent)

    def test_ignores_outside_match(self):
        data = packet()
        data["map"]["game_state"] = "DOTA_GAMERULES_STATE_POST_GAME"
        self.assertIsNone(parse_state(data))

    def test_death_trigger_and_throttle(self):
        coach = Coach(role=3, voice=False)
        self.assertEqual(coach.update(packet(), now=100), "advised")
        self.assertEqual(coach.update(packet(61, alive=False), now=130), "advised")
        self.assertEqual(coach.status()["message_source"], "off")
        self.assertNotIn("复活后", coach.message)
        self.assertEqual(coach.update(packet(62, alive=False), now=131), "received")
        self.assertEqual(coach.update(packet(63, alive=True), now=132), "received")
        self.assertEqual(coach.update(packet(154, alive=True), now=223), "advised")

    def test_major_item_and_cooldown(self):
        coach = Coach(role=2, voice=False)
        coach.update(packet(), now=100)
        self.assertEqual(coach.update(packet(90, item="item_blink"), now=130), "advised")
        self.assertEqual(coach.reason, "major_item")
        self.assertNotIn("关键装备", coach.message)

    def test_shorter_cadence_and_resource_event(self):
        coach = Coach(role=None, voice=False)
        self.assertEqual(coach.update(packet(), now=100), "advised")
        self.assertEqual(coach.update(packet(clock=104), now=144), "received")
        self.assertEqual(coach.update(packet(clock=105), now=145), "advised")
        low = packet(clock=123)
        low["hero"]["health"] = 300
        self.assertEqual(coach.update(low, now=163), "advised")
        self.assertEqual(coach.reason, "low_health")

    def test_lane_estimate_is_observational(self):
        self.assertEqual(map_region(-5000, 0), "top")
        self.assertEqual(map_region(-300, -400), "mid")
        self.assertEqual(map_region(-6000, -6000), "radiant_base")
        self.assertEqual(map_region(6000, 6000), "dire_base")
        self.assertEqual(map_region(-2000, 2000), "river")
        self.assertEqual(map_region(-2600, -1000), "radiant_side")
        self.assertEqual(lane_type("top", "radiant"), "offlane")
        self.assertEqual(lane_type("top", "dire"), "safelane")
        coach = Coach(role=None, voice=False)
        for i in range(6):
            data = packet(clock=10 + i * 5)
            data["player"]["team_name"] = "radiant"
            data["hero"].update({"xpos": -5000, "ypos": 0})
            coach.update(data, now=100 + i * 5)
        self.assertEqual(coach.status()["starting_lane"], "offlane")

    def test_beginner_group_only_and_conventional_role_inference(self):
        self.assertEqual(infer_role("safelane", "core"), 1)
        self.assertEqual(infer_role("mid", "core"), 2)
        self.assertEqual(infer_role("offlane", "core"), 3)
        self.assertEqual(infer_role("offlane", "support"), 4)
        self.assertEqual(infer_role("safelane", "support"), 5)
        self.assertIsNone(infer_role("mid", "support"))
        self.assertIsNone(infer_role("unknown", "core"))
        coach = Coach(role=None, voice=False)
        coach.update(packet(), now=100)
        self.assertEqual(coach.status()["message_source"], "off")
        self.assertNotIn("兵线", coach.message)

    def test_ai_request_uses_selected_fields_and_response_text(self):
        response = {"output": [{"type": "message", "content": [
            {"type": "output_text", "text": "先看小地图，再跟队友行动。"}]}],
            "usage": {"input_tokens": 120, "output_tokens": 35, "total_tokens": 155}}
        captured = {}

        def fake_urlopen(request, timeout):
            captured.update(json.loads(request.data))
            self.assertEqual(timeout, 12)
            return io.BytesIO(json.dumps(response).encode())

        with patch("coach.open_model_request", fake_urlopen):
            advice = request_ai_advice(parse_state(packet()), "regular", 3, "test-model", "test-key")
        self.assertEqual(advice.message, "先看小地图，再跟队友行动。")
        self.assertEqual((advice.input_tokens, advice.output_tokens, advice.total_tokens), (120, 35, 155))
        self.assertEqual(captured["model"], "test-model")
        self.assertFalse(captured["store"])
        fields = json.loads(captured["input"])
        self.assertNotIn("match_id", fields)
        self.assertEqual(fields["role_1_to_5"], 3)
        self.assertEqual(fields["role_source"], "unknown")

    def test_openrouter_chat_request_uses_context(self):
        response = {"model": "deepseek/test", "usage": {"prompt_tokens": 140, "completion_tokens": 80,
                    "total_tokens": 220},
                    "choices": [{"message": {"content": "先收安全兵线；大招已就绪，随后观察队友是否能配合推进。"}}]}
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data)
            return io.BytesIO(json.dumps(response).encode())

        with patch("coach.open_model_request", fake_urlopen):
            message = request_ai_advice(parse_state(packet()), "regular", 1, "deepseek/test", "test-key",
                                        role_group="core", role_source="lane_group_inference",
                                        provider="openrouter", context={"starting_lane_inferred": "safelane"})
        self.assertIn("安全兵线", message.message)
        self.assertEqual((message.input_tokens, message.output_tokens, message.total_tokens), (140, 80, 220))
        self.assertEqual(message.model, "deepseek/test")
        self.assertEqual(captured["url"], "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(captured["body"]["reasoning_effort"], "none")
        self.assertEqual(captured["body"]["max_completion_tokens"], 700)
        fields = json.loads(captured["body"]["messages"][1]["content"])
        self.assertEqual(fields["role_1_to_5"], 1)
        self.assertEqual(fields["starting_lane_inferred"], "safelane")
        self.assertNotIn("match_id", fields)

    def test_live_ai_settings_do_not_return_key(self):
        coach = Coach(role=None, voice=False)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(coach))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
            connection.request("POST", "/ai-settings", body=json.dumps({
                "provider": "deepseek", "model": "deepseek-flash", "api_key": "secret-test-key"}).encode())
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 200)
            self.assertEqual(coach.status()["ai_provider"], "deepseek")
            self.assertTrue(coach.status()["ai_ready"])
            connection.request("GET", "/status")
            body = connection.getresponse().read().decode()
            self.assertNotIn("secret-test-key", body)
            connection.close()
        finally:
            server.shutdown()
            server.server_close()

    def test_only_model_provides_tactical_advice(self):
        def fake_urlopen(request, timeout):
            return io.BytesIO(json.dumps({"usage": {"prompt_tokens": 150, "completion_tokens": 60,
                "total_tokens": 210}, "choices": [{"message": {
                "content": "先安全吃这一波经验；大招可用，再观察队友位置决定是否支援。"}}]}).encode())

        with patch("coach.open_model_request", fake_urlopen):
            coach = Coach(role=None, voice=False, ai_model="deepseek-flash",
                          api_key="test-key", ai_provider="deepseek")
            coach.update(packet(), now=100)
            deadline = time.monotonic() + 2
            while coach.status()["ai_pending"] and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertEqual(coach.status()["message_source"], "model")
        self.assertIn("安全吃这一波", coach.status()["message"])
        self.assertEqual(coach.status()["last_token_usage"]["total_tokens"], 210)
        self.assertEqual(coach.status()["session_token_usage"]["total_tokens"], 210)
        self.assertEqual(coach.status()["usage_reported_calls"], 1)

    def test_empty_model_answer_still_counts_reported_tokens(self):
        def fake_urlopen(request, timeout):
            return io.BytesIO(json.dumps({"usage": {"prompt_tokens": 120, "completion_tokens": 700,
                "total_tokens": 820}, "choices": [{"finish_reason": "length",
                "message": {"content": ""}}]}).encode())

        with patch("coach.open_model_request", fake_urlopen):
            coach = Coach(role=None, voice=False, ai_model="test-model",
                          api_key="test-key", ai_provider="openrouter")
            coach.update(packet(), now=100)
            deadline = time.monotonic() + 2
            while coach.status()["ai_pending"] and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertEqual(coach.status()["message_source"], "error")
        self.assertIn("模型未生成建议", coach.status()["message"])
        self.assertEqual(coach.status()["last_token_usage"]["total_tokens"], 820)
        self.assertEqual(coach.status()["usage_reported_calls"], 1)

    def test_pause_keeps_gsi_updates_and_resume_advises(self):
        coach = Coach(role=2, voice=False)
        coach.update(packet(clock=100), now=100)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(coach))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
            connection.request("POST", "/coach-control", body=b'{"paused":true}')
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 200)
            paused_sequence = coach.status()["advice_seq"]
            self.assertEqual(coach.update(packet(clock=200, level=6), now=200), "paused")
            status = coach.status()
            self.assertEqual(status["state"]["clock"], 200)
            self.assertTrue(status["coach_paused"])
            self.assertEqual(status["advice_seq"], paused_sequence)
            self.assertEqual(status["message_source"], "paused")
            connection.request("POST", "/coach-control", body=b'{"paused":false}')
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 200)
            self.assertEqual(coach.update(packet(clock=201, level=6), now=201), "advised")
            self.assertFalse(coach.status()["coach_paused"])
            connection.close()
        finally:
            server.shutdown()
            server.server_close()

    def test_pause_discards_inflight_model_advice(self):
        started = threading.Event()
        release = threading.Event()

        def fake_urlopen(request, timeout):
            started.set()
            self.assertTrue(release.wait(2))
            return io.BytesIO(json.dumps({"usage": {"prompt_tokens": 10,
                "completion_tokens": 20, "total_tokens": 30},
                "choices": [{"message": {"content": "应被丢弃的模型建议"}}]}).encode())

        with patch("coach.open_model_request", fake_urlopen):
            coach = Coach(role=None, voice=False, ai_model="test-model",
                          api_key="test-key", ai_provider="openrouter")
            coach.update(packet(), now=100)
            self.assertTrue(started.wait(2))
            coach.set_paused(True)
            release.set()
            deadline = time.monotonic() + 2
            while coach.status()["usage_reported_calls"] == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
        status = coach.status()
        self.assertEqual(status["message_source"], "paused")
        self.assertNotIn("应被丢弃", status["message"])
        self.assertEqual(status["session_token_usage"]["total_tokens"], 30)

    def test_manual_question_uses_filtered_gsi_and_reports_tokens(self):
        sent = []

        def fake_urlopen(request, timeout):
            sent.append(json.loads(request.data))
            return io.BytesIO(json.dumps({"usage": {"prompt_tokens": 20,
                "completion_tokens": 30, "total_tokens": 50},
                "choices": [{"message": {"content": "先确认兵线位置，再决定是否支援。"}}]}).encode())

        coach = Coach(role=None, voice=False, ai_model="test-model",
                      api_key="test-key", ai_provider="openrouter")
        with patch("coach.open_model_request", fake_urlopen):
            code, _ = coach.ask("现在要去支援吗？")
            self.assertEqual(code, 202)
            deadline = time.monotonic() + 2
            while coach.status()["question_pending"] and time.monotonic() < deadline:
                time.sleep(0.01)
        status = coach.status()
        self.assertEqual(status["questions"][-1]["status"], "done")
        self.assertIn("兵线", status["questions"][-1]["answer"])
        self.assertEqual(status["session_token_usage"]["total_tokens"], 50)
        self.assertIn("现在要去支援吗", sent[0]["messages"][1]["content"])
        self.assertNotIn("match_id", sent[0]["messages"][1]["content"])

    def test_manual_question_receives_remembered_enemy_roster_without_enemy_position(self):
        sent = []

        def fake_urlopen(request, timeout):
            sent.append(json.loads(request.data))
            return io.BytesIO(json.dumps({"choices": [{"message": {
                "content": "狙击手通常有较强后期输出，但当前装备和发育未知。"}}]}).encode())

        data = packet(clock=100)
        data["player"]["team_name"] = "radiant"
        data["minimap"] = {"enemy": {"unitname": "npc_dota_hero_sniper",
            "image": "minimap_enemyicon", "team": 3, "xpos": 300, "ypos": 400}}
        coach = Coach(role=None, voice=False, ai_provider="openrouter")
        coach.update(data, now=time.monotonic())
        data["minimap"] = {}
        coach.update(data, now=time.monotonic())
        with coach.lock:
            coach.ai_model = "test-model"
            coach.api_key = "test-key"
        with patch("coach.open_model_request", fake_urlopen):
            self.assertEqual(coach.ask("对面谁偏后期？")[0], 202)
            deadline = time.monotonic() + 2
            while coach.status()["question_pending"] and time.monotonic() < deadline:
                time.sleep(0.01)
        fields = json.loads(sent[0]["messages"][1]["content"])
        enemies = fields["match_roster_from_observed_gsi"]["enemies"]
        self.assertEqual(enemies[0]["hero"], "npc_dota_hero_sniper")
        self.assertNotIn("xpos", enemies[0])
        self.assertEqual(fields["match_roster_from_observed_gsi"]["enemies_unknown"], 4)
        self.assertEqual(fields["visible_enemies_from_gsi_minimap"], [])
        self.assertNotIn("match_id", fields)

    def test_current_visible_enemy_region_reaches_advice_and_question_without_coordinates(self):
        sent = []

        def fake_urlopen(request, timeout):
            sent.append(json.loads(request.data))
            return io.BytesIO(json.dumps({"choices": [{"message": {
                "content": "巫医刚在上路露头，留意他可能移动。"}}]}).encode())

        data = packet(clock=100)
        data["player"]["team_name"] = "radiant"
        data["minimap"] = {"enemy": {"unitname": "npc_dota_hero_witch_doctor",
            "image": "minimap_enemyicon", "team": 3, "xpos": -5000, "ypos": 4000}}
        with patch("coach.open_model_request", fake_urlopen):
            coach = Coach(role=None, voice=False, ai_model="test-model",
                          api_key="test-key", ai_provider="openrouter")
            coach.update(data, now=time.monotonic())
            deadline = time.monotonic() + 2
            while coach.status()["ai_pending"] and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(coach.ask("巫医现在在哪？")[0], 202)
            deadline = time.monotonic() + 2
            while coach.status()["question_pending"] and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertEqual(len(sent), 2)
        for request in sent:
            fields = json.loads(request["messages"][1]["content"])
            self.assertEqual(fields["visible_enemies_from_gsi_minimap"],
                             [{"hero_schinese": coach.localizer.hero("npc_dota_hero_witch_doctor"),
                               "region_inferred": "top"}])
            self.assertNotIn("-5000", request["messages"][1]["content"])
            self.assertNotIn("match_id", fields)

    def test_manual_question_respects_pause_and_input_validation(self):
        coach = Coach(role=None, voice=False, ai_model="test-model",
                      api_key="test-key", ai_provider="openrouter")
        coach.set_paused(True)
        self.assertEqual(coach.ask("可以提问吗？")[0], 409)
        coach.set_paused(False)
        self.assertEqual(coach.ask(" ")[0], 400)
        self.assertEqual(coach.ask("x" * 501)[0], 400)
        self.assertEqual(coach.status()["questions"], [])

    def test_role_can_be_set_during_match(self):
        coach = Coach(role=None, voice=False)
        coach.update(packet(), now=100)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(coach))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
            connection.request("POST", "/settings", body=b'{"role": 2}')
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 200)
            self.assertEqual(coach.status()["role"], 2)
            self.assertEqual(coach.status()["message_source"], "off")
            connection.request("POST", "/settings", body=b'{"lane": "mid"}')
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 200)
            self.assertEqual(coach.status()["role"], 2)
            self.assertEqual(coach.status()["effective_lane"], "mid")
            connection.request("POST", "/settings", body=b'{"role_group": "core"}')
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 200)
            status = coach.status()
            self.assertIsNone(status["role"])
            self.assertEqual(status["effective_role"], 2)
            self.assertEqual(status["role_source"], "lane_group_inference")
            self.assertNotIn("2 号位", status["message"])
            connection.request("POST", "/settings", body=b'{"role_group": "support"}')
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 200)
            self.assertIsNone(coach.status()["effective_role"])
            self.assertEqual(coach.status()["message_source"], "off")
            connection.close()
        finally:
            server.shutdown()
            server.server_close()

    def test_official_names_are_separate_from_raw_ids(self):
        names = Localizer({
            "npc_dota_hero_windrunner:n": "风行者",
            "dota_tooltip_ability_item_tango": "树之祭祀",
        })
        coach = Coach(role=None, voice=False, localizer=names)
        coach.update(packet(item="item_tango"), now=100)
        status = coach.status()
        self.assertEqual(status["hero_zh"], "axe")
        self.assertEqual(status["items_zh"], ["树之祭祀"])
        self.assertEqual(status["state"]["items"], ("item_tango",))


if __name__ == "__main__":
    unittest.main()
