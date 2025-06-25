import re
import json
import time
from bs4 import BeautifulSoup

from .base import BaseModule
from config import log

class Module(BaseModule):
    """
    Checks for and collects completed task rewards for a village.
    """
    def tick(self, village_data):
        agent = self.agent
        
        try:
            dorf_resp = agent.client.sess.get(f"{agent.client.server_url}/dorf1.php?newdid={agent.village_id}", timeout=15)
            soup = BeautifulSoup(dorf_resp.text, 'html.parser')

            if not soup.select_one('#sidebarBoxQuestmaster .claimable'):
                return

        except Exception as e:
            log.error(f"[{agent.client.username}] Failed to check for questmaster icon in {agent.village_name}: {e}")
            return

        log.info(f"[{agent.client.username}] Claimable tasks found for village {agent.village_name}. Checking tasks page...")

        try:
            tasks_page_resp = agent.client.sess.get(f"{agent.client.server_url}/tasks?t=1&villageId={agent.village_id}", timeout=15)
            tasks_soup = BeautifulSoup(tasks_page_resp.text, 'html.parser')

            script_tag = tasks_soup.find("script", string=re.compile(r"window\.Travian\.React\.Tasks\.render"))
            if not script_tag:
                log.warning(f"[{agent.client.username}] Could not find the tasks data script on the page for {agent.village_name}.")
                return

            script_content = script_tag.string
            tasks_data_start = script_content.find('tasksData:')
            if tasks_data_start == -1:
                log.warning(f"[{agent.client.username}] Could not find 'tasksData:' in the script for {agent.village_name}.")
                return

            json_start = script_content.find('{', tasks_data_start)
            if json_start == -1:
                log.warning(f"[{agent.client.username}] Could not find opening brace for tasksData JSON in {agent.village_name}.")
                return
            
            open_braces = 0
            json_end = -1
            for i, char in enumerate(script_content[json_start:]):
                if char == '{':
                    open_braces += 1
                elif char == '}':
                    open_braces -= 1
                    if open_braces == 0:
                        json_end = json_start + i + 1
                        break
            
            if json_end == -1:
                log.warning(f"[{agent.client.username}] Could not find matching closing brace for tasksData JSON in {agent.village_name}.")
                return

            json_str = script_content[json_start:json_end]
            tasks_data = json.loads(json_str)
            
            all_tasks = tasks_data.get('generalTasks', []) + tasks_data.get('activeVillageTasks', [])

            for task in all_tasks:
                for level in task.get('levels', []):
                    if level.get('readyToBeCollected'):
                        log.info(f"[{agent.client.username}] Found claimable task in {agent.village_name}: {task.get('name')} - {level.get('title')}")
                        
                        payload = { "questId": level.get('questId') }

                        agent.client.collect_task_reward(payload, agent.village_id, agent.village_name)
                        time.sleep(1)

        except Exception as e:
            log.error(f"[{agent.client.username}] An error occurred while collecting tasks for {agent.village_name}: {e}", exc_info=True)