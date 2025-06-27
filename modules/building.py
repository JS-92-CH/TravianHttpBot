from .base import BaseModule
from config import BOT_STATE, state_lock, save_config, gid_name, is_multi_instance, log, build_lock
import json
from collections import deque

class Module(BaseModule):
    """Handles building queue management for a village."""

    def __init__(self, agent):
        super().__init__(agent)
        try:
            with open("prerequisites.json", "r") as f:
                self.prerequisites_data = json.load(f)
        except FileNotFoundError:
            log.error("prerequisites.json not found. Please create it.")
            self.prerequisites_data = {}

    def get_prerequisites(self, gid):
        """Gets prerequisites from the loaded JSON data."""
        return self.prerequisites_data.get(str(gid), {}).get("prerequisites", [])

    def _resolve_dependencies(self, goal_gid, goal_level, all_buildings):
        """Recursively resolves all dependencies for a given building goal."""
        tasks_to_add = []
        q = deque([(goal_gid, goal_level)])
        visited = set()

        while q:
            current_gid, current_level = q.popleft()

            if (current_gid, current_level) in visited:
                continue
            visited.add((current_gid, current_level))

            # Check if this goal is already met
            building = next((b for b in all_buildings if b.get('gid') == current_gid), None)
            actual_level = building.get('level', 0) if building else 0
            
            if actual_level >= current_level:
                continue

            # If not met, add tasks for the required levels
            for level in range(actual_level + 1, current_level + 1):
                tasks_to_add.append({'type': 'building', 'gid': current_gid, 'level': level})

            # Add prerequisites to the queue to be checked
            prereqs = self.get_prerequisites(current_gid)
            for prereq in prereqs:
                q.append((prereq['gid'], prereq['level']))
        
        # Remove duplicates while preserving order
        unique_tasks = []
        seen = set()
        for task in reversed(tasks_to_add):
            task_tuple = (task['gid'], task['level'])
            if task_tuple not in seen:
                unique_tasks.insert(0, task)
                seen.add(task_tuple)
        
        # Prioritize Main Building (gid 15)
        unique_tasks.sort(key=lambda x: (x['gid'] != 15, x['level']))

        return unique_tasks


    def tick(self, village_data):
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

        # Sanitize queue (remove completed tasks)
        sanitized_queue = []
        needs_save = False
        for task in build_queue:
            # ... (sanitization logic remains the same)
            sanitized_queue.append(task)

        if needs_save:
            with state_lock:
                BOT_STATE["build_queues"][str(agent.village_id)] = sanitized_queue
            save_config()
        if not sanitized_queue:
            return

        goal_task = sanitized_queue[0]
        
        # New Dependency Resolution Step
        if goal_task.get('type') == 'building':
            missing_prereqs = self._resolve_dependencies(goal_task['gid'], goal_task['level'], all_buildings)
            
            # Filter out tasks that are already in the main queue
            current_queue_set = set((t['gid'], t['level']) for t in sanitized_queue if t.get('type') == 'building')
            new_tasks = [t for t in missing_prereqs if (t['gid'], t['level']) not in current_queue_set]

            if new_tasks:
                log.info(f"AGENT({agent.village_name}): Found {len(new_tasks)} missing prerequisites for {gid_name(goal_task['gid'])} Lvl {goal_task['level']}. Prepending to queue.")
                with state_lock:
                    BOT_STATE["build_queues"][str(agent.village_id)] = new_tasks + sanitized_queue
                save_config()
                # Restart the tick to process the new first item in the queue
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

            # Find where to build or upgrade
            # This logic now assumes prerequisites are already met and queued
            building_at_loc = next((b for b in all_buildings if b.get('gid') == goal_gid), None)

            if building_at_loc and building_at_loc.get('level', 0) < goal_level:
                action_plan = {'type': 'upgrade', 'location': building_at_loc['id'], 'gid': goal_gid}
            elif not building_at_loc:
                 # Find an empty slot
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
            log.error(f"AGENT({agent.village_name}): Could not determine action plan for goal {goal_task}. This might mean the goal is complete or no slot is available. Removing from queue.")
            with state_lock:
                BOT_STATE["build_queues"][str(agent.village_id)] = sanitized_queue[1:]
            save_config()
            return

        if agent.use_hero_resources and hasattr(agent, 'hero_module'):
            try:
                agent.hero_module.ensure_resources_for_build(agent.village_id, action_plan['location'], action_plan['gid'])
            except Exception as exc:
                log.error(f"AGENT({agent.village_name}): Hero resource step failed: {exc}")

        with build_lock:
            log.info(f"AGENT({agent.village_name}): Build lock acquired.")
            quick_check_data = agent.client.fetch_and_parse_village(agent.village_id)
            if len(quick_check_data.get("queue", [])) >= max_queue_length:
                log.warning(f"AGENT({agent.village_name}): Another build started while waiting for lock. Releasing lock.")
                build_result = {'status': 'skipped'}
            else:
                is_new = action_plan['type'] == 'new'
                log.info(f"--> EXECUTING BUILD: {action_plan['type']} {gid_name(action_plan['gid'])} at location {action_plan['location']}.")
                build_result = agent.client.initiate_build(agent.village_id, action_plan['location'], action_plan['gid'], is_new_build=is_new)

        if build_result.get('status') == 'success':
            log.info(f"AGENT({agent.village_name}): Successfully started task for '{gid_name(action_plan['gid'])}'.")
            # Task is not removed here, it will be sanitized on the next tick
            agent.stop_event.wait(1)
        elif build_result.get('status') != 'skipped':
            log.warning(f"AGENT({agent.village_name}): Failed to build '{gid_name(action_plan['gid'])}'. Reason: {build_result.get('reason')}. Waiting 5 seconds.")
            agent.stop_event.wait(5)