import os
import json
from collections import deque
from .base import BaseModule
from config import BOT_STATE, state_lock, save_config, gid_name, log
from threading import Semaphore

class Module(BaseModule):
    """Handles building queue management for a village."""

    def __init__(self, agent):
        super().__init__(agent)
        # --- Start of Changes ---
        # Construct an absolute path to the prerequisites file
        try:
            # Get the directory where this script is located
            script_dir = os.path.dirname(os.path.abspath(__file__))
            # Go one level up to the project root
            project_root = os.path.dirname(script_dir)
            # Construct the full path to prerequisites.json
            prereq_path = os.path.join(project_root, "prerequisites.json")
            
            with open(prereq_path, "r") as f:
                self.prerequisites_data = json.load(f)
            if not self.prerequisites_data:
                 log.warning("prerequisites.json is empty.")
        except FileNotFoundError:
            log.error(f"FATAL: prerequisites.json not found at expected path: {prereq_path}")
            self.prerequisites_data = {}
        except json.JSONDecodeError:
            log.error("FATAL: prerequisites.json is not a valid JSON file.")
            self.prerequisites_data = {}
        # --- End of Changes ---

    def get_prerequisites(self, gid):
        """Gets prerequisites from the loaded JSON data."""
        return self.prerequisites_data.get(str(gid), {}).get("prerequisites", [])

    def _resolve_dependencies(self, goal_gid, goal_level, all_buildings, current_queue):
        """
        Recursively resolves all dependencies for a given building goal,
        considering both existing buildings and items already in the queue.
        """
        tasks_to_add = []
        q = deque([(goal_gid, goal_level)])
        visited = set()
        
        queued_set = set((t['gid'], t['level']) for t in current_queue if t.get('type') == 'building')

        while q:
            current_gid, current_level = q.popleft()

            if (current_gid, current_level) in visited:
                continue
            visited.add((current_gid, current_level))

            building = next((b for b in all_buildings if b.get('gid') == current_gid), None)
            actual_level = building.get('level', 0) if building else 0
            
            is_satisfied = actual_level >= current_level or any(
                q_gid == current_gid and q_level >= current_level for q_gid, q_level in queued_set
            )
            
            if is_satisfied:
                continue

            highest_known_level = actual_level
            for q_gid, q_level in queued_set:
                 if q_gid == current_gid:
                      highest_known_level = max(highest_known_level, q_level)

            for level in range(highest_known_level + 1, current_level + 1):
                tasks_to_add.append({'type': 'building', 'gid': current_gid, 'level': level})

            prereqs = self.get_prerequisites(current_gid)
            for prereq in prereqs:
                q.append((prereq['gid'], prereq['level']))
        
        unique_tasks = []
        seen = set()
        for task in reversed(tasks_to_add):
            task_tuple = (task['gid'], task['level'])
            if task_tuple not in seen:
                unique_tasks.insert(0, task)
                seen.add(task_tuple)
        
        unique_tasks.sort(key=lambda x: (x['gid'] != 15, x['level']))

        return unique_tasks


    def tick(self, village_data, build_semaphore: Semaphore):
        agent = self.agent
        max_queue_length = 2 if agent.use_dual_queue else 1
        active_builds = village_data.get("queue", [])

        if len(active_builds) >= max_queue_length:
            wait_time = active_builds[0].get('eta', 60) + 1
            log.info(f"AGENT({agent.village_name}): Construction queue full ({len(active_builds)}/{max_queue_length}). Waiting for {wait_time:.2f}s.")
            agent.stop_event.wait(wait_time)
            return

        with state_lock:
            build_queue = BOT_STATE["build_queues"].get(str(agent.village_id), [])[:]

        if not build_queue:
            agent.stop_event.wait(60)
            return

        all_buildings = village_data.get("buildings", [])

        sanitized_queue = []
        needs_save = False
        for task in build_queue:
            if task.get('type') == 'building':
                building = next((b for b in all_buildings if b.get('gid') == task['gid']), None)
                if building and building.get('level', 0) >= task['level']:
                    log.info(f"AGENT({agent.village_name}): Task '{gid_name(task['gid'])}' Lvl {task['level']} already completed. Removing from queue.")
                    needs_save = True
                    continue
            sanitized_queue.append(task)

        if needs_save:
            with state_lock:
                BOT_STATE["build_queues"][str(agent.village_id)] = sanitized_queue
            save_config()
        
        if not sanitized_queue:
            return

        goal_task = sanitized_queue[0]
        
        if goal_task.get('type') == 'building':
            missing_prereqs = self._resolve_dependencies(goal_task['gid'], goal_task['level'], all_buildings, sanitized_queue)
            
            if missing_prereqs:
                log.info(f"AGENT({agent.village_name}): Found {len(missing_prereqs)} missing prerequisites for {gid_name(goal_task['gid'])} Lvl {goal_task['level']}. Prepending to queue.")
                with state_lock:
                    original_queue = BOT_STATE["build_queues"].get(str(agent.village_id), [])
                    BOT_STATE["build_queues"][str(agent.village_id)] = missing_prereqs + original_queue
                save_config()
                return

        action_plan = None

        if goal_task.get('type') == 'resource_plan':
            target_level = goal_task.get('level')
            resource_fields = sorted([b for b in all_buildings if 1 <= b['id'] <= 18], key=lambda x: (x.get('level', 0), x['id']))
            next_field_to_upgrade = next((field for field in resource_fields if field.get('level', 0) < target_level), None)

            if next_field_to_upgrade:
                log.info(f"AGENT({agent.village_name}): Resource plan: Upgrading {gid_name(next_field_to_upgrade['gid'])} at Loc {next_field_to_upgrade['id']} to Lvl {next_field_to_upgrade['level']+1}.")
                action_plan = {'type': 'upgrade', 'location': next_field_to_upgrade['id'], 'gid': next_field_to_upgrade['gid']}
            else:
                log.info(f"AGENT({agent.village_name}): Resource plan to level {target_level} is complete. Removing task.")
                with state_lock:
                    BOT_STATE["build_queues"][str(agent.village_id)] = sanitized_queue[1:]
                save_config()
                return
        
        else: # 'building' type
            goal_gid, goal_level = goal_task.get('gid'), goal_task.get('level')
            log.info(f"AGENT({agent.village_name}): Next goal is '{gid_name(goal_gid)}' to Lvl {goal_level}.")

            building_at_loc = next((b for b in all_buildings if b.get('gid') == goal_gid), None)

            if building_at_loc and building_at_loc.get('level', 0) < goal_level:
                action_plan = {'type': 'upgrade', 'location': building_at_loc['id'], 'gid': goal_gid}
            elif not building_at_loc:
                WALL_GIDS = [31, 32, 33, 42, 43]
                forced_location = None
                if goal_gid == 16: forced_location = 39
                elif goal_gid in WALL_GIDS: forced_location = 40
                
                if forced_location and any(b['id'] == forced_location and b['gid'] == 0 for b in all_buildings):
                     action_plan = {'type': 'new', 'location': forced_location, 'gid': goal_gid}
                else:
                    empty_slot = next((b for b in all_buildings if b['id'] > 18 and b['gid'] == 0 and b['id'] not in [39, 40]), None)
                    if empty_slot:
                        action_plan = {'type': 'new', 'location': empty_slot['id'], 'gid': goal_gid}

        if not action_plan:
            log.error(f"AGENT({agent.village_name}): Could not determine action plan for goal {goal_task}. Goal might be complete or no slot is available. Removing from queue.")
            with state_lock:
                BOT_STATE["build_queues"][str(agent.village_id)] = sanitized_queue[1:]
            save_config()
            return

        if agent.use_hero_resources and hasattr(agent, 'hero_module'):
            try:
                agent.hero_module.ensure_resources_for_build(agent.village_id, action_plan['location'], action_plan['gid'])
            except Exception as exc:
                log.error(f"AGENT({agent.village_name}): Hero resource step failed: {exc}")

        with build_semaphore:
            log.info(f"AGENT({agent.village_name}): Build semaphore acquired.")
            quick_check_data = agent.client.fetch_and_parse_village(agent.village_id)
            if len(quick_check_data.get("queue", [])) >= max_queue_length:
                log.warning(f"AGENT({agent.village_name}): Another build started while waiting for semaphore. Releasing.")
                build_result = {'status': 'skipped'}
            else:
                is_new = action_plan['type'] == 'new'
                log.info(f"--> EXECUTING BUILD: {action_plan['type']} {gid_name(action_plan['gid'])} at location {action_plan['location']}.")
                build_result = agent.client.initiate_build(agent.village_id, action_plan['location'], action_plan['gid'], is_new_build=is_new)

        if build_result.get('status') == 'success':
            log.info(f"AGENT({agent.village_name}): Successfully started task for '{gid_name(action_plan['gid'])}'.")
            agent.stop_event.wait(1)
        elif build_result.get('status') != 'skipped':
            log.warning(f"AGENT({agent.village_name}): Failed to build '{gid_name(action_plan['gid'])}'. Reason: {build_result.get('reason')}. Waiting 5 seconds.")
            agent.stop_event.wait(5)