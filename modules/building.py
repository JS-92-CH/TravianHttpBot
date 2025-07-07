# modules/building.py

from .base import BaseModule
from config import BOT_STATE, state_lock, save_config, gid_name, log, is_multi_instance
import json
import time
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
        
        with state_lock:
            build_queue = BOT_STATE["build_queues"].get(str(agent.village_id), [])[:]

        if not build_queue:
            return 0

        all_buildings = village_data.get("buildings", [])
        active_builds = village_data.get("queue", [])
        goal_task = build_queue[0]
        action_plan = None
        
        if goal_task.get('type') == 'resource_plan':
            target_level = goal_task.get('level')
            resource_fields = sorted([b for b in all_buildings if 1 <= b['id'] <= 18], key=lambda x: (x.get('level', 0), x['id']))
            
            field_to_upgrade = next((field for field in resource_fields if field.get('level', 0) < target_level), None)

            if not field_to_upgrade:
                log.info(f"AGENT({agent.village_name}): Resource plan to level {target_level} is complete. Removing task.")
                with state_lock: BOT_STATE["build_queues"][str(agent.village_id)] = build_queue[1:]
                save_config()
                return 10

            action_plan = {'type': 'upgrade', 'location': field_to_upgrade['id'], 'gid': field_to_upgrade['gid'], 'is_new': False}
            log.info(f"AGENT({agent.village_name}): Resource plan: Upgrading {gid_name(action_plan['gid'])} at Loc {action_plan['location']} to Lvl {field_to_upgrade['level']+1}.")
        
        elif goal_task.get('type') == 'building':
            goal_gid = goal_task.get('gid')
            goal_level = goal_task.get('level')
            goal_location = goal_task.get('location')

            # --- START OF NEW LOGIC ---
            target_building_on_map = next((b for b in all_buildings if b.get('id') == goal_location), None)
            
            if not target_building_on_map:
                 log.error(f"AGENT({agent.village_name}): Building at location {goal_location} not found in village data. This should not happen.")
                 return 0

            current_level = target_building_on_map.get('level', 0)
            is_new_build = target_building_on_map.get('gid', 0) == 0

            # Calculate the effective level by including queued upgrades for this specific building.
            # This is an approximation based on name, as the server queue doesn't provide a location ID.
            building_name_in_queue = gid_name(goal_gid)
            queued_levels = [int(build.get('level')) for build in active_builds if building_name_in_queue in build.get('name')]
            effective_level = max([current_level] + queued_levels)

            if effective_level >= goal_level:
                log.info(f"AGENT({agent.village_name}): Task '{gid_name(goal_gid)}' Lvl {goal_level} at Loc {goal_location} is already complete (Effective Lvl: {effective_level}). Removing from queue.")
                with state_lock: BOT_STATE["build_queues"][str(agent.village_id)] = build_queue[1:]
                save_config()
                return 10 
            
            # If the slot is empty, we must build a new building.
            if is_new_build:
                missing_prereqs = self._resolve_dependencies(goal_gid, all_buildings)
                if missing_prereqs:
                    log.info(f"AGENT({agent.village_name}): Prepending prerequisites for new building {gid_name(goal_gid)}.")
                    with state_lock: BOT_STATE["build_queues"][str(agent.village_id)] = missing_prereqs + build_queue
                    save_config()
                    return 0
                action_plan = {'type': 'new', 'location': goal_location, 'gid': goal_gid, 'is_new': True}
            else: # The slot is not empty, so we are upgrading.
                action_plan = {'type': 'upgrade', 'location': goal_location, 'gid': goal_gid, 'is_new': False}
            # --- END OF NEW LOGIC ---

        else:
            log.error(f"AGENT({agent.village_name}): Unknown task type '{goal_task.get('type')}'. Removing task.")
            with state_lock: BOT_STATE["build_queues"][str(agent.village_id)] = build_queue[1:]
            save_config()
            return 0
        
        if action_plan:
            if agent.use_hero_resources and hasattr(agent, 'resources_module') and agent.resources_module:
                log.info(f"AGENT({agent.village_name}): Checking resources for {gid_name(action_plan['gid'])} with hero resource usage enabled.")
                
                used_items = agent.resources_module.ensure_resources_for_build(
                    village_id=agent.village_id, 
                    slot_id=action_plan['location'], 
                    gid=action_plan['gid']
                )
                
                if used_items:
                    log.info(f"AGENT({agent.village_name}): Used hero resources. Pausing for 2 seconds before re-attempting build.")
                    time.sleep(2)
        
        build_result = agent.client.initiate_build(agent.village_id, action_plan['location'], action_plan['gid'], is_new_build=action_plan['is_new'])

        if build_result.get('status') == 'success':
            return build_result.get('eta', 300)
        else:
            log.warning(f"AGENT({agent.village_name}): Failed to build '{gid_name(action_plan['gid'])}'. Reason: {build_result.get('reason')}.")
            # On failure, we don't want to get stuck in a fast loop.
            # The agent's main loop will set a longer wait time.
            return 0