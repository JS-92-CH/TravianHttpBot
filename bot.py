import asyncio
import time
from threading import Thread, Event
from config import BOT_STATE, state_lock, log, save_config
from client import Client

class BotManager(Thread):
    def __init__(self, socketio_instance):
        super().__init__()
        self.stop_event = Event()
        self.socketio = socketio_instance
        self.agents = {}

    def run(self):
        log.info("Bot Manager started.")
        self.socketio.emit("log_message", {'data': "Bot Manager started."})
        
        asyncio.run(self.main_loop())

    async def main_loop(self):
        while not self.stop_event.is_set():
            with state_lock:
                accounts = list(BOT_STATE['accounts'])
            
            for account_config in accounts:
                username = account_config['username']
                if username not in self.agents:
                    self.agents[username] = AccountAgent(account_config, self.socketio)
                    self.agents[username].start()
            
            # Clean up stopped agents
            stopped_agents = [u for u, a in self.agents.items() if not a.is_alive()]
            for username in stopped_agents:
                del self.agents[username]

            await asyncio.sleep(5) # Check for new accounts every 5 seconds
            
        # Stop all running agents
        for agent in self.agents.values():
            agent.stop()
        for agent in self.agents.values():
            agent.join()
        log.info("Bot Manager stopped.")
        self.socketio.emit("log_message", {'data': "Bot Manager stopped."})


    def stop(self):
        self.stop_event.set()
        log.info("Stopping Bot Manager...")


class AccountAgent(Thread):
    def __init__(self, account_config, socketio):
        super().__init__()
        self.account_config = account_config
        self.socketio = socketio
        self.client = Client(account_config['username'], account_config['password'], account_config['server_url'])
        self.stop_event = Event()
        self.village_agents = {}

    def run(self):
        log.info(f"Agent for account {self.account_config['username']} started.")
        asyncio.run(self.manage_villages())

    async def manage_villages(self):
        if not await self.client.login():
            log.error(f"Could not start agent for {self.account_config['username']}, login failed.")
            return

        # Fetch tribe once per account
        player_tribe = await self.client.get_player_tribe()
        with state_lock:
             if 'account_data' not in BOT_STATE:
                BOT_STATE['account_data'] = {}
             BOT_STATE['account_data'][self.account_config['username']] = {'tribe': player_tribe}


        while not self.stop_event.is_set():
            villages = await self.client.get_villages()
            with state_lock:
                BOT_STATE['village_data'][self.account_config['username']] = villages
            
            for village in villages:
                village_id = village['id']
                if village_id not in self.village_agents:
                    log.info(f"Starting agent for village {village['name']} ({village_id})")
                    agent = VillageAgent(village_id, self.client, self.socketio)
                    agent.start()
                    self.village_agents[village_id] = agent
            
            # Clean up stopped village agents
            stopped_villages = [vid for vid, a in self.village_agents.items() if not a.is_alive()]
            for vid in stopped_villages:
                del self.village_agents[vid]
            
            await asyncio.sleep(300) # Re-check village list every 5 minutes
            
        # Stop all village agents
        for agent in self.village_agents.values():
            agent.stop()
        for agent in self.village_agents.values():
            agent.join()
        await self.client.close_session()

    def stop(self):
        self.stop_event.set()


class VillageAgent(Thread):
    def __init__(self, village_id, client, socketio):
        super().__init__()
        self.village_id = village_id
        self.client = client
        self.socketio = socketio
        self.stop_event = Event()

    def run(self):
        log.info(f"Village agent {self.village_id} started.")
        asyncio.run(self.task_loop())

    async def task_loop(self):
        while not self.stop_event.is_set():
            try:
                # 1. Update village state
                data = await self.client.get_village_data(self.village_id)
                if data:
                    with state_lock:
                        BOT_STATE['village_data'][self.village_id] = data
                        # Add tribe info to village data for easy access on frontend
                        username = self.client.username
                        if username in BOT_STATE.get('account_data', {}):
                           BOT_STATE['village_data'][self.village_id]['tribe'] = BOT_STATE['account_data'][username].get('tribe', 'unknown')


                    self.socketio.emit('state_update', BOT_STATE)
                
                # 2. Check build queue
                with state_lock:
                    queue = list(BOT_STATE['build_queues'].get(self.village_id, []))
                
                if queue:
                    current_goal = queue[0]
                    
                    # Sanitize: check if goal is already met
                    if self.is_goal_met(current_goal, data['buildings']):
                        log.info(f"Goal {current_goal} already met. Removing from queue.")
                        self.remove_from_queue(0)
                        continue

                    # Check for prerequisites
                    prereqs = await self.check_prerequisites(current_goal, data['buildings'])
                    if prereqs:
                        log.info(f"Found prerequisites for {current_goal}: {prereqs}")
                        # Prepend prerequisites to the queue
                        self.prepend_to_queue(prereqs)
                        continue # Restart the loop to process the new first item

                    # Attempt to build
                    if await self.can_build(current_goal, data['resources']):
                        log.info(f"Attempting to build {current_goal}")
                        success = await self.client.initiate_build(self.village_id, current_goal['location'], current_goal['gid'])
                        if success:
                            # Assuming build takes time, just wait.
                            # A better approach would be to check the construction list.
                            log.info("Build initiated. Waiting for next cycle.")
                        else:
                            log.warning("Build command failed. Will retry.")
                    else:
                        log.info(f"Not enough resources for {current_goal}. Waiting.")

            except Exception as e:
                log.error(f"Error in VillageAgent {self.village_id} loop: {e}")

            await asyncio.sleep(60) # Wait 60 seconds before next cycle

    def is_goal_met(self, goal, buildings):
        """Check if the building goal is already achieved."""
        if goal['type'] == 'building':
            for b in buildings:
                if b['id'] == goal['location']:
                    # For a new build, gid must match. For an upgrade, id is enough.
                    if b['gid'] == 0: # It's an empty slot, so goal is not met
                         return False
                    if b['gid'] == goal['gid'] and b['level'] >= goal['level']:
                        return True
                    # If GID is different, it means another building is there. This is a conflict.
                    # For simplicity, we'll let the user handle this. Bot will get stuck.
        return False
        
    async def check_prerequisites(self, goal, buildings):
        """Check for and return any missing prerequisites for a building goal."""
        # This is a simplified check. A real implementation would parse the build page.
        # For this example, we'll assume the client can fetch them.
        gid_to_build = goal.get('gid')
        # Find an empty slot if location is not fixed
        target_location = goal.get('location')
        if not target_location:
            for b in buildings:
                if b['id'] > 18 and b['gid'] == 0:
                    target_location = b['id']
                    break
        if not target_location:
            log.warning("No empty slots to build prerequisites.")
            return [] # No empty slots

        # Let's assume a function get_prerequisites(gid) exists
        required = await self.client.get_prerequisites(self.village_id, target_location)
        missing = []
        for req in required:
            is_met = False
            for b in buildings:
                if b['gid'] == req['gid'] and b['level'] >= req['level']:
                    is_met = True
                    break
            if not is_met:
                # Find an empty slot for this prerequisite
                prereq_location = None
                for b_slot in buildings:
                     if b_slot['id'] > 18 and b_slot['gid'] == 0 and b_slot['id'] != target_location:
                         prereq_location = b_slot['id']
                         break
                if prereq_location:
                    missing.append({'type': 'building', 'gid': req['gid'], 'level': req['level'], 'location': prereq_location})
        return missing

    def prepend_to_queue(self, tasks):
        with state_lock:
            BOT_STATE['build_queues'].setdefault(self.village_id, []).insert(0, *tasks)
        save_config()
        self.socketio.emit('state_update', BOT_STATE)

    def remove_from_queue(self, index):
        with state_lock:
            if self.village_id in BOT_STATE['build_queues']:
                del BOT_STATE['build_queues'][self.village_id][index]
        save_config()
        self.socketio.emit('state_update', BOT_STATE)
        
    async def can_build(self, goal, resources):
        # This is a placeholder. A real implementation would fetch costs.
        return True

    def stop(self):
        self.stop_event.set()