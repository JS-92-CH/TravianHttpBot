import time
import threading
from typing import Dict, Optional

from client import TravianClient
from config import log, BOT_STATE, state_lock, save_config, load_default_build_queue, gid_name, is_multi_instance

build_lock = threading.Lock()

class VillageAgent(threading.Thread):
    def __init__(self, client: TravianClient, village_info: Dict, socketio_instance, use_dual_queue: bool = False):
        super().__init__()
        self.client = client
        self.village_id = village_info['id']
        self.village_name = village_info['name']
        self.socketio = socketio_instance
        self.use_dual_queue = use_dual_queue
        self.stop_event = threading.Event()
        self.daemon = True

    def stop(self):
        self.stop_event.set()

    def run(self):
        log.info(f"Agent started for village: {self.village_name} ({self.village_id}) - Dual Queue: {self.use_dual_queue}")
        while not self.stop_event.is_set():
            try:
                village_data = self.client.fetch_and_parse_village(self.village_id)
                if not village_data:
                    self.stop_event.wait(300); continue

                with state_lock:
                    BOT_STATE["village_data"][str(self.village_id)] = village_data
                self.socketio.emit("state_update", BOT_STATE)

                max_queue_length = 2 if self.use_dual_queue else 1
                active_builds = village_data.get("queue", [])
                
                if len(active_builds) >= max_queue_length:
                    wait_time = active_builds[0].get('eta', 60) + 1
                    log.info(f"AGENT({self.village_name}): Construction queue full ({len(active_builds)}/{max_queue_length}). Waiting for {wait_time:.2f}s.")
                    self.stop_event.wait(wait_time)
                    continue

                with state_lock:
                    build_queue = BOT_STATE["build_queues"].get(str(self.village_id), [])[:]
                
                if not build_queue:
                    self.stop_event.wait(60); continue
                
                all_buildings = village_data.get("buildings", [])
                
                sanitized_queue = []
                needs_save = False
                for task in build_queue:
                    if task.get('type') == 'resource_plan':
                        sanitized_queue.append(task)
                        continue
                    goal_gid, goal_level = task.get('gid'), task.get('level')
                    existing_instances = [b for b in all_buildings if b['gid'] == goal_gid]
                    if not is_multi_instance(goal_gid) and any(b['level'] >= goal_level for b in existing_instances):
                        log.info(f"AGENT({self.village_name}): Sanitizing. Goal '{gid_name(goal_gid)} Lvl {goal_level}' is complete. Purging.")
                        needs_save = True
                    else:
                        sanitized_queue.append(task)
                if needs_save:
                    with state_lock: BOT_STATE["build_queues"][str(self.village_id)] = sanitized_queue
                    save_config()
                if not sanitized_queue: continue

                goal_task = sanitized_queue[0]

                if goal_task.get('type') == 'resource_plan':
                    target_level = goal_task.get('level')
                    resource_fields = sorted([b for b in all_buildings if 1 <= b['id'] <= 18], key=lambda x: x['level'])
                    
                    next_field_to_upgrade = next((field for field in resource_fields if field.get('level', 0) < target_level), None)

                    if next_field_to_upgrade:
                        log.info(f"AGENT({self.village_name}): Resource plan active. Next is {gid_name(next_field_to_upgrade['gid'])} Lvl {next_field_to_upgrade['level']+1}.")
                        new_task = {'gid': next_field_to_upgrade['gid'], 'level': next_field_to_upgrade['level'] + 1, 'location': next_field_to_upgrade['id'], 'type': 'building'}
                        with state_lock:
                            BOT_STATE["build_queues"][str(self.village_id)] = [new_task] + sanitized_queue
                        save_config()
                        self.stop_event.wait(5)
                        continue
                    else:
                        log.info(f"AGENT({self.village_name}): Resource plan to level {target_level} is complete. Removing task.")
                        with state_lock:
                            BOT_STATE["build_queues"][str(self.village_id)] = sanitized_queue[1:]
                        save_config()
                        continue

                goal_gid, goal_level = goal_task.get('gid'), goal_task.get('level')
                log.info(f"AGENT({self.village_name}): Next goal is '{gid_name(goal_gid)}' to Lvl {goal_level}.")
                
                action_plan = None
                
                if is_multi_instance(goal_gid):
                    candidates = [b for b in all_buildings if b['gid'] == goal_gid and b['level'] < 20]
                    if candidates:
                        candidates.sort(key=lambda x: x['level'], reverse=True)
                        building_to_upgrade = candidates[0]
                        if building_to_upgrade['level'] < goal_level:
                                action_plan = {'type': 'upgrade', 'location': building_to_upgrade['id'], 'gid': goal_gid}
                    else:
                        empty_slot = next((b for b in all_buildings if b['id'] > 18 and b['gid'] == 0), None)
                        if empty_slot:
                            action_plan = {'type': 'new', 'location': empty_slot['id'], 'gid': goal_gid}
                else: 
                    existing_building = next((b for b in all_buildings if b['gid'] == goal_gid), None)
                    if existing_building:
                        action_plan = {'type': 'upgrade', 'location': existing_building['id'], 'gid': goal_gid}
                    else:
                        WALL_GIDS = [31, 32, 33, 42, 43]
                        forced_location = None
                        if goal_gid == 16: forced_location = 39
                        elif goal_gid in WALL_GIDS: forced_location = 40

                        if forced_location:
                            if any(b['id'] == forced_location and b['gid'] == 0 for b in all_buildings):
                                action_plan = {'type': 'new', 'location': forced_location, 'gid': goal_gid}
                        else:
                            empty_slot = next((b for b in all_buildings if b['id'] > 18 and b['gid'] == 0 and b['id'] not in [39, 40]), None)
                            if empty_slot: action_plan = {'type': 'new', 'location': empty_slot['id'], 'gid': goal_gid}

                if not action_plan:
                    log.error(f"AGENT({self.village_name}): Could not determine action plan for {gid_name(goal_gid)}. Village may be full. Skipping task for now.")
                    self.stop_event.wait(300); continue

                prereqs = self.client.get_prerequisites(self.village_id, action_plan['location'], goal_gid)
                existing_gids_map = {b['gid']: b for b in all_buildings if b['gid'] != 0}
                missing_prereqs = [req for req in prereqs if not (b := existing_gids_map.get(req['gid'])) or b['level'] < req['level']]
                
                if missing_prereqs:
                    new_tasks = [{'type': 'building', 'gid': r['gid'], 'level': r['level']} for r in missing_prereqs]
                    log.warning(f"AGENT({self.village_name}): Prepending prerequisites for '{gid_name(goal_gid)}'.")
                    with state_lock: BOT_STATE["build_queues"][str(self.village_id)] = new_tasks + sanitized_queue
                    save_config(); self.stop_event.wait(5); continue

                with build_lock:
                    log.info(f"AGENT({self.village_name}): Build lock acquired.")
                    quick_check_data = self.client.fetch_and_parse_village(self.village_id)
                    if len(quick_check_data.get("queue", [])) >= max_queue_length:
                        log.warning(f"AGENT({self.village_name}): Another build started while waiting for lock. Releasing lock.")
                        build_result = {'status': 'skipped'}
                    else:
                        is_new = action_plan['type'] == 'new'
                        log.info(f"--> EXECUTING BUILD: {action_plan['type']} {gid_name(goal_gid)} at location {action_plan['location']}.")
                        build_result = self.client.initiate_build(self.village_id, action_plan['location'], goal_gid, is_new_build=is_new)

                if build_result.get('status') == 'success':
                    log.info(f"AGENT({self.village_name}): Successfully started task for '{gid_name(goal_gid)}'.")
                    self.stop_event.wait(10)
                elif build_result.get('status') != 'skipped':
                    log.warning(f"AGENT({self.village_name}): Failed to build '{gid_name(goal_gid)}'. Reason: {build_result.get('reason')}. Waiting 5 seconds.")
                    self.stop_event.wait(5)
                    
            except Exception as e:
                log.error(f"Agent for village {self.village_name} CRITICAL ERROR: {e}", exc_info=True)
                self.stop_event.wait(300)

        log.info(f"Agent stopped for village: {self.village_name} ({self.village_id})")


class BotManager(threading.Thread):
    def __init__(self, socketio_instance):
        super().__init__()
        self.socketio = socketio_instance
        self.stop_event = threading.Event()
        self.village_agents: Dict[int, VillageAgent] = {}
        self.daemon = True

    def stop(self):
        log.info("Stopping all village agents...")
        for agent in self.village_agents.values(): agent.stop()
        for agent in self.village_agents.values(): agent.join()
        self.stop_event.set()
        log.info("All agents stopped.")

    def run(self):
        log.info("Bot Manager started.")
        while not self.stop_event.is_set():
            with state_lock: accounts = BOT_STATE["accounts"][:]
            if not accounts: self.stop_event.wait(15); continue
            
            for account in accounts:
                if self.stop_event.is_set(): break
                client = TravianClient(account["username"], account["password"], account["server_url"])
                if not client.login(): self.stop_event.wait(60); continue
                
                try:
                    resp = client.sess.get(f"{client.server_url}/dorf1.php", timeout=15)
                    sidebar_data = client.parse_village_page(resp.text, "dorf1")
                    villages = sidebar_data.get("villages", [])
                    with state_lock: BOT_STATE["village_data"][client.username] = villages
                    
                    current_ids = {v['id'] for v in villages}
                    for vid in list(self.village_agents.keys()):
                        if vid not in current_ids:
                            self.village_agents.pop(vid).stop()
                            log.info(f"Stopped and removed agent for stale village ID: {vid}")

                    use_dual_queue = account.get("use_dual_queue", False)
                    for village in villages:
                        if agent := self.village_agents.get(village['id']):
                            agent.use_dual_queue = use_dual_queue
                        else:
                            log.info(f"Creating new agent for {village['name']}")
                            agent = VillageAgent(client, village, self.socketio, use_dual_queue)
                            self.village_agents[village['id']] = agent
                            agent.start()

                except Exception as exc:
                    log.error(f"Failed to manage agents for {account['username']}: {exc}", exc_info=True)
            
            self.stop_event.wait(300)
        log.info("Bot Manager stopped.")