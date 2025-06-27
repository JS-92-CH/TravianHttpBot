from .base import BaseModule
from config import BOT_STATE, state_lock, save_config, gid_name, log, build_lock
import json
from collections import deque

class Module(BaseModule):
    """Handles building queue management for a village."""

    def __init__(self, agent):
        super().__init__(agent)
        try:
            with open("prerequisites.json", "r") as f:
                self.prerequisites_data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            log.error(f"Could not load or parse prerequisites.json: {e}. Dependency checks will be skipped.")
            self.prerequisites_data = {}

    def get_prerequisites(self, gid):
        """Gets the single list of prerequisites for constructing a building."""
        return self.prerequisites_data.get(str(gid), {}).get("prerequisites", [])

    def _resolve_dependencies(self, goal_gid, all_buildings):
        """
        Checks if the initial construction prerequisites for a goal are met.
        If not, returns a list of tasks for the missing prerequisites.
        """
        tasks_to_add = []
        prereqs = self.get_prerequisites(goal_gid)

        for prereq in prereqs:
            prereq_gid = prereq['gid']
            prereq_level = prereq['level']
            
            existing_building = next((b for b in all_buildings if b.get('gid') == prereq_gid), None)
            actual_level = existing_building.get('level', 0) if existing_building else 0

            if actual_level < prereq_level:
                log.info(f"Dependency for {gid_name(goal_gid)} not met: {gid_name(prereq_gid)} needs Lvl {prereq_level}, is at {actual_level}.")
                tasks_to_add.append({'type': 'building', 'gid': prereq_gid, 'level': prereq_level})
        
        return tasks_to_add

    def tick(self, village_data):
        agent = self.agent
        max_queue_length = 2 if agent.use_dual_queue else 1
        active_builds = village_data.get("queue", [])

        if len(active_builds) >= max_queue_length:
            return 0

        with state_lock:
            build_queue = BOT_STATE["build_queues"].get(str(agent.village_id), [])[:]

        if not build_queue:
            return 0

        all_buildings = village_data.get("buildings", [])
        goal_task = build_queue[0]
        
        # --- HANDLE RESOURCE PLAN ---
        if goal_task.get('type') == 'resource_plan':
            target_level = goal_task.get('level')
            resource_fields = sorted([b for b in all_buildings if 1 <= b['id'] <= 18], key=lambda x: (x.get('level', 0), x['id']))
            
            field_to_upgrade = next((field for field in resource_fields if field.get('level', 0) < target_level), None)

            if not field_to_upgrade:
                log.info(f"AGENT({agent.village_name}): Resource plan to level {target_level} is complete. Removing task.")
                with state_lock: BOT_STATE["build_queues"][str(agent.village_id)] = build_queue[1:]
                save_config()
                return 0

            action_plan = {'type': 'upgrade', 'location': field_to_upgrade['id'], 'gid': field_to_upgrade['gid'], 'is_new': False}
            log.info(f"AGENT({agent.village_name}): Resource plan: Upgrading {gid_name(action_plan['gid'])} at Loc {action_plan['location']} to Lvl {field_to_upgrade['level']+1}.")
        
        # --- HANDLE BUILDING PLAN ---
        elif goal_task.get('type') == 'building':
            goal_gid = goal_task.get('gid')
            goal_level = goal_task.get('level')
            
            WALL_GIDS = {31, 32, 33, 42, 43}
            existing_building = None
            if goal_gid in WALL_GIDS:
                existing_building = next((b for b in all_buildings if b.get('gid') in WALL_GIDS), None)
            else:
                existing_building = next((b for b in all_buildings if b.get('gid') == goal_gid), None)


            if existing_building and existing_building.get('level', 0) >= goal_level:
                log.info(f"AGENT({agent.village_name}): Goal '{gid_name(goal_gid)}' Lvl {goal_level} is complete. Removing from queue.")
                with state_lock: BOT_STATE["build_queues"][str(agent.village_id)] = build_queue[1:]
                save_config()
                return 0

            if not existing_building:
                missing_prereqs = self._resolve_dependencies(goal_gid, all_buildings)
                if missing_prereqs:
                    log.info(f"AGENT({agent.village_name}): Prepending prerequisites for new building {gid_name(goal_gid)}.")
                    with state_lock: BOT_STATE["build_queues"][str(agent.village_id)] = missing_prereqs + build_queue
                    save_config()
                    return 0

                slot_id = None
                if goal_gid == 16: slot_id = 39
                elif goal_gid in WALL_GIDS: slot_id = 40
                
                if slot_id and any(b['id'] == slot_id and b['gid'] == 0 for b in all_buildings):
                    location = slot_id
                else:
                    empty_slot = next((b for b in all_buildings if b['id'] > 18 and b['gid'] == 0 and b['id'] not in [39, 40]), None)
                    location = empty_slot['id'] if empty_slot else None
                
                if not location:
                    log.error(f"AGENT({agent.village_name}): No empty slot for {gid_name(goal_gid)}. Removing task.")
                    with state_lock: BOT_STATE["build_queues"][str(agent.village_id)] = build_queue[1:]
                    save_config()
                    return 0
                action_plan = {'type': 'new', 'location': location, 'gid': goal_gid, 'is_new': True}
            else:
                action_plan = {'type': 'upgrade', 'location': existing_building['id'], 'gid': goal_gid, 'is_new': False}
        else:
            log.error(f"AGENT({agent.village_name}): Unknown task type '{goal_task.get('type')}'. Removing task.")
            with state_lock: BOT_STATE["build_queues"][str(agent.village_id)] = build_queue[1:]
            save_config()
            return 0
            
        # --- EXECUTE BUILD ---
        with build_lock:
            quick_check_data = agent.client.fetch_and_parse_village(agent.village_id)
            if len(quick_check_data.get("queue", [])) >= max_queue_length:
                return 0

            build_result = agent.client.initiate_build(agent.village_id, action_plan['location'], action_plan['gid'], is_new_build=action_plan['is_new'])

        if build_result.get('status') == 'success':
            return build_result.get('eta', 300)
        else:
            log.warning(f"AGENT({agent.village_name}): Failed to build '{gid_name(action_plan['gid'])}'. Reason: {build_result.get('reason')}.")
            return 0