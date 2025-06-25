from .base import BaseModule
from config import BOT_STATE, state_lock, save_config, gid_name, is_multi_instance, log, build_lock

class Module(BaseModule):
    """Handles building queue management for a village."""

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

        # Sanitize queue
        sanitized_queue = []
        needs_save = False
        for task in build_queue:
            if task.get('type') == 'resource_plan':
                sanitized_queue.append(task)
                continue

            goal_gid, goal_level = task.get('gid'), task.get('level')
            is_complete = False

            if 'location' in task and task['location'] > 0:
                building = next((b for b in all_buildings if b['id'] == task['location']), None)
                if building and building.get('gid') == goal_gid and building.get('level', 0) >= goal_level:
                    is_complete = True
                    log.info(f"AGENT({agent.village_name}): Sanitizing. Goal '{gid_name(goal_gid)} Lvl {goal_level} at Loc {task['location']}' is complete. Purging.")
            elif not is_multi_instance(goal_gid):
                if any(b.get('gid') == goal_gid and b.get('level', 0) >= goal_level for b in all_buildings):
                    is_complete = True
                    log.info(f"AGENT({agent.village_name}): Sanitizing. Generic goal '{gid_name(goal_gid)} Lvl {goal_level}' is complete. Purging.")

            if is_complete:
                needs_save = True
            else:
                sanitized_queue.append(task)

        if needs_save:
            with state_lock:
                BOT_STATE["build_queues"][str(agent.village_id)] = sanitized_queue
            save_config()
        if not sanitized_queue:
            return

        goal_task = sanitized_queue[0]
        action_plan = None

        if goal_task.get('type') == 'resource_plan':
            target_level = goal_task.get('level')
            resource_fields = sorted([b for b in all_buildings if 1 <= b['id'] <= 18], key=lambda x: (x.get('level', 0), x['id']))
            next_field_to_upgrade = next((field for field in resource_fields if field.get('level', 0) < target_level), None)

            if next_field_to_upgrade:
                log.info(f"AGENT({agent.village_name}): Resource plan: Upgrading {gid_name(next_field_to_upgrade['gid'])} at Loc {next_field_to_upgrade['id']} to Lvl {next_field_to_upgrade['level']+1}.")
                action_plan = {'type': 'upgrade', 'location': next_field_to_upgrade['id'], 'gid': next_field_to_upgrade['gid']}
                goal_gid = action_plan['gid']
            else:
                log.info(f"AGENT({agent.village_name}): Resource plan to level {target_level} is complete. Removing task.")
                with state_lock:
                    BOT_STATE["build_queues"][str(agent.village_id)] = sanitized_queue[1:]
                save_config()
                return
        else:
            goal_gid, goal_level = goal_task.get('gid'), goal_task.get('level')
            log.info(f"AGENT({agent.village_name}): Next goal is '{gid_name(goal_gid)}' to Lvl {goal_level}.")

            goal_loc = goal_task.get('location')
            if goal_loc:
                building_at_loc = next((b for b in all_buildings if b['id'] == goal_loc), None)
                if building_at_loc:
                    action_plan = {'type': 'new' if building_at_loc['gid'] == 0 else 'upgrade', 'location': goal_loc, 'gid': goal_gid}
            else:
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
                    candidates = [b for b in all_buildings if b['gid'] == goal_gid and b['level'] < goal_level]
                    if candidates:
                        candidates.sort(key=lambda x: x['level'])
                        action_plan = {'type': 'upgrade', 'location': candidates[0]['id'], 'gid': goal_gid}
                    else:
                        if not any(b['gid'] == goal_gid for b in all_buildings):
                            WALL_GIDS = [31, 32, 33, 42, 43]
                            forced_location = None
                            if goal_gid == 16:
                                forced_location = 39
                            elif goal_gid in WALL_GIDS:
                                forced_location = 40
                            if forced_location:
                                if any(b['id'] == forced_location and b['gid'] == 0 for b in all_buildings):
                                    action_plan = {'type': 'new', 'location': forced_location, 'gid': goal_gid}
                            else:
                                empty_slot = next((b for b in all_buildings if b['id'] > 18 and b['gid'] == 0 and b['id'] not in [39, 40]), None)
                                if empty_slot:
                                    action_plan = {'type': 'new', 'location': empty_slot['id'], 'gid': goal_gid}

        if not action_plan:
            log.error(f"AGENT({agent.village_name}): Could not determine action plan for goal {goal_task}. Village may be full or goal is already met. Skipping task for now.")
            agent.stop_event.wait(300)
            return

        prereqs = agent.client.get_prerequisites(agent.village_id, action_plan['location'], goal_gid)
        existing_gids_map = {b['gid']: b for b in all_buildings if b['gid'] != 0}
        missing_prereqs = [req for req in prereqs if not (b := existing_gids_map.get(req['gid'])) or b['level'] < req['level']]

        if missing_prereqs:
            new_tasks = [{'type': 'building', 'gid': r['gid'], 'level': r['level']} for r in missing_prereqs]
            log.warning(f"AGENT({agent.village_name}): Prepending prerequisites for '{gid_name(goal_gid)}'.")
            with state_lock:
                BOT_STATE["build_queues"][str(agent.village_id)] = new_tasks + sanitized_queue
            save_config()
            agent.stop_event.wait(5)
            return

        with build_lock:
            log.info(f"AGENT({agent.village_name}): Build lock acquired.")
            quick_check_data = agent.client.fetch_and_parse_village(agent.village_id)
            if len(quick_check_data.get("queue", [])) >= max_queue_length:
                log.warning(f"AGENT({agent.village_name}): Another build started while waiting for lock. Releasing lock.")
                build_result = {'status': 'skipped'}
            else:
                is_new = action_plan['type'] == 'new'
                log.info(f"--> EXECUTING BUILD: {action_plan['type']} {gid_name(goal_gid)} at location {action_plan['location']}.")
                build_result = agent.client.initiate_build(agent.village_id, action_plan['location'], goal_gid, is_new_build=is_new)

        if build_result.get('status') == 'success':
            log.info(f"AGENT({agent.village_name}): Successfully started task for '{gid_name(goal_gid)}'.")
            if goal_task.get('type') != 'resource_plan':
                with state_lock:
                    BOT_STATE["build_queues"][str(agent.village_id)] = sanitized_queue[1:]
                save_config()
            agent.stop_event.wait(1)
        elif build_result.get('status') != 'skipped':
            log.warning(f"AGENT({agent.village_name}): Failed to build '{gid_name(goal_gid)}'. Reason: {build_result.get('reason')}. Waiting 5 seconds.")
            agent.stop_event.wait(5)

