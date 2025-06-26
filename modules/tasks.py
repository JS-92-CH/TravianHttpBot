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
            # Check for the claimable tasks icon on the main page.
            dorf_resp = agent.client.sess.get(f"{agent.client.server_url}/dorf1.php?newdid={agent.village_id}", timeout=15)
            soup = BeautifulSoup(dorf_resp.text, 'html.parser')

            if not soup.select_one('#sidebarBoxQuestmaster .claimable'):
                return

        except Exception as e:
            log.error(f"[{agent.client.username}] Failed to check for questmaster icon in {agent.village_name}: {e}")
            return

        log.info(f"[{agent.client.username}] Claimable tasks icon found for village {agent.village_name}. Checking tasks page...")

        try:
            tasks_page_resp = agent.client.sess.get(f"{agent.client.server_url}/tasks?t=1&villageId={agent.village_id}", timeout=15)
            tasks_soup = BeautifulSoup(tasks_page_resp.text, 'html.parser')

            # --- REVISED LOGIC ---

            # 1. Find all 'Collect' buttons that are not disabled/already collected.
            claimable_task_names = []
            task_overview = tasks_soup.find('div', class_='taskOverview')
            if task_overview:
                # This selector directly finds buttons that are ready to be clicked.
                collect_buttons = task_overview.select('button.collect:not(.collected):not(.disabled)')
                
                for button in collect_buttons:
                    # Find the parent '.task' container for this button to get the title.
                    task_container = button.find_parent(class_='task')
                    if task_container:
                        title_div = task_container.find('div', class_='title')
                        if title_div:
                            claimable_task_names.append(title_div.text.strip())

            if not claimable_task_names:
                log.warning(f"[{agent.client.username}] No visually claimable tasks found for {agent.village_name}, although sidebar icon was present. The rewards may be on another tab (e.g., 'General Tasks').")
                return

            log.info(f"[{agent.client.username}] Found visually claimable tasks from HTML: {list(set(claimable_task_names))}")

            # 2. Parse the JSON data to get the questId for the claimable tasks.
            script_tag = tasks_soup.find("script", string=re.compile(r"window\.Travian\.React\.Tasks\.render"))
            if not script_tag:
                log.warning(f"[{agent.client.username}] Could not find tasks data script in {agent.village_name}.")
                return

            script_content = script_tag.string
            tasks_data_start = script_content.find('tasksData:')
            if tasks_data_start == -1: return

            json_start = script_content.find('{', tasks_data_start)
            if json_start == -1: return
            
            open_braces = 0
            json_end = -1
            for i, char in enumerate(script_content[json_start:]):
                if char == '{': open_braces += 1
                elif char == '}':
                    open_braces -= 1
                    if open_braces == 0:
                        json_end = json_start + i + 1
                        break
            
            if json_end == -1: return

            json_str = script_content[json_start:json_end]
            tasks_data = json.loads(json_str)
            
            all_tasks = tasks_data.get('generalTasks', []) + tasks_data.get('activeVillageTasks', [])

            # 3. Collect rewards for tasks that match our visually confirmed list.
            collected_quests = set()
            for task in all_tasks:
                task_name_from_json = task.get('name')
                
                # We check if the task's title/name from the JSON matches one we found in the HTML.
                if task_name_from_json in claimable_task_names:
                    for level in task.get('levels', []):
                        quest_id = level.get('questId')
                        if level.get('readyToBeCollected') and quest_id not in collected_quests:
                            log.info(f"[{agent.client.username}] Collecting reward in {agent.village_name}: {task_name_from_json} - {level.get('title')}")
                            
                            payload = { "questId": quest_id }

                            if agent.client.collect_task_reward(payload, agent.village_id, agent.village_name):
                                collected_quests.add(quest_id) # Avoid trying to collect the same questId again
                                time.sleep(1.5) # Wait a moment after successful collection

        except Exception as e:
            log.error(f"[{agent.client.username}] An error occurred while collecting tasks for {agent.village_name}: {e}", exc_info=True)