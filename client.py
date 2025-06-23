import re
from aiohttp import ClientSession
from bs4 import BeautifulSoup

from config import log

class Client:
    def __init__(self, username, password, server_url):
        self.username = username
        self.password = password
        self.base_url = server_url
        self.session = None
        self.logged_in = False
        self.last_login_attempt = None

    async def _get_session(self):
        if not self.session or self.session.closed:
            self.session = ClientSession()
        return self.session

    async def login(self):
        session = await self._get_session()
        login_url = f"{self.base_url}/login.php"
        try:
            # First, GET the login page to get any session cookies and the login token
            async with session.get(login_url) as response:
                response.raise_for_status()
                content = await response.text()
                # with open("login_page_debug.html", "w", encoding="utf-8") as f:
                #     f.write(content)
                
                soup = BeautifulSoup(content, 'html.parser')
                login_token_input = soup.find('input', {'name': 's1'})
                if not login_token_input:
                    login_token_input = soup.find('input', {'name': 'login'})

                if not login_token_input:
                    log.error("Could not find login token on the login page.")
                    return False
                login_token = login_token_input['value']
                log.info(f"Found login token: {login_token}")


            login_data = {
                'name': self.username,
                'password': self.password,
                's1': 'Login',
                'w': '1920:1080',
                'login': login_token
            }
            
            async with session.post(login_url, data=login_data, allow_redirects=True) as response:
                response.raise_for_status()
                content = await response.text()

                if "Authentication failed" in content or "Wrong password" in content:
                    log.error(f"Login failed for {self.username}. Check credentials.")
                    self.logged_in = False
                    return False
                
                # Check for redirection to dorf1.php or similar, which indicates successful login
                if "dorf1.php" in str(response.url) or "dorf2.php" in str(response.url):
                    self.logged_in = True
                    log.info(f"Successfully logged in as {self.username}")
                    return True
                else:
                    log.error(f"Login failed for {self.username}. Unexpected page content.")
                    self.logged_in = False
                    # with open("login_fail_page.html", "w", encoding="utf-8") as f:
                    #     f.write(content)
                    return False
        except Exception as e:
            log.error(f"An error occurred during login for {self.username}: {e}")
            self.logged_in = False
        return False
        
    async def get_player_tribe(self):
        """Fetches the player's tribe from their profile page."""
        if not self.logged_in:
            await self.login()
        
        session = await self._get_session()
        profile_url = f"{self.base_url}/spieler.php" # URL of the profile page
        
        try:
            async with session.get(profile_url) as response:
                response.raise_for_status()
                content = await response.text()
                soup = BeautifulSoup(content, 'html.parser')
                
                # Travian Kingdoms and newer versions often use a body class
                body_tag = soup.find('body')
                if body_tag and 'tribe' in str(body_tag.get('class', '')):
                    for css_class in body_tag['class']:
                        if 'tribe' in css_class:
                            tribe_id = int(re.search(r'\d+', css_class).group())
                            return {1: 'roman', 2: 'teuton', 3: 'gaul', 6: 'egyptian', 7: 'hun'}.get(tribe_id, 'unknown')

                # Fallback for other versions that might have tribe info in a specific div
                tribe_div = soup.find('div', class_='tribe')
                if tribe_div:
                    for css_class in tribe_div['class']:
                        if css_class.startswith('tribe'):
                            tribe_id = int(css_class.replace('tribe', ''))
                            return {1: 'roman', 2: 'teuton', 3: 'gaul', 6: 'egyptian', 7: 'hun'}.get(tribe_id, 'unknown')
                            
                log.warning("Could not determine tribe from profile page.")
                return "unknown"

        except Exception as e:
            log.error(f"Error fetching player tribe: {e}")
            return "unknown"


    async def get_villages(self):
        if not self.logged_in:
            await self.login()
        session = await self._get_session()
        villages_url = f"{self.base_url}/dorf1.php"
        try:
            async with session.get(villages_url) as response:
                response.raise_for_status()
                content = await response.text()
                soup = BeautifulSoup(content, 'html.parser')
                village_list = soup.find('div', {'id': 'villageList'})
                if not village_list:
                    return []
                villages = []
                for a in village_list.find_all('a'):
                    href = a.get('href')
                    if 'newdid' in href:
                        village_id = href.split('newdid=')[1].split('&')[0]
                        name = a.find('div', class_='name').text
                        villages.append({'id': village_id, 'name': name})
                return villages
        except Exception as e:
            log.error(f"Error fetching village list: {e}")
            return []

    async def get_village_data(self, village_id):
        if not self.logged_in:
            await self.login()
        session = await self._get_session()
        village_data = {
            'resources': {}, 'storage': {}, 'production': {}, 'buildings': []
        }
        try:
            # Fetch dorf1 for resource fields
            async with session.get(f"{self.base_url}/dorf1.php?newdid={village_id}") as response:
                content = await response.text()
                village_data.update(self._parse_dorf1_html(content))
            # Fetch dorf2 for city buildings
            async with session.get(f"{self.base_url}/dorf2.php?newdid={village_id}") as response:
                content = await response.text()
                village_data['buildings'].extend(self._parse_dorf2_html(content))
            # Fetch resource production details
            async with session.get(f"{self.base_url}/dorf1.php?newdid={village_id}") as response:
                content = await response.text()
                village_data.update(self._parse_production(content))
            return village_data
        except Exception as e:
            log.error(f"Error fetching data for village {village_id}: {e}")
            return None
    
    def _parse_production(self, html_content):
        soup = BeautifulSoup(html_content, 'html.parser')
        data = {'production': {}}
        for i in range(1, 5):
            res_element = soup.find(id=f'l{i}')
            if res_element:
                title = res_element.get('title', '0')
                # Extract the number from the title attribute
                match = re.search(r'(\d+)', title)
                if match:
                    data['production'][f'l{i}'] = int(match.group(1))
        return data

    def _parse_dorf1_html(self, html_content):
        soup = BeautifulSoup(html_content, 'html.parser')
        data = {'buildings': []}
        # Resource fields are typically divs with class 'buildingSlot' and id 'slotX'
        for i in range(1, 19): # Resource fields are slots 1-18
            slot = soup.find('div', {'data-aid': str(i)})
            if slot:
                level_tag = slot.find('div', class_='labelLayer')
                level = int(level_tag.text) if level_tag else 0
                gid = int(slot.get('data-gid')) if slot.get('data-gid') else 0
                data['buildings'].append({'id': i, 'level': level, 'gid': gid})
        
        # Parse resources and storage
        data['resources'] = {f'l{i}': int(float(soup.find(id=f'l{i}').text.replace(',', '').replace('.', ''))) for i in range(1, 5)}
        storage = {}
        warehouse = soup.find(id='stockBarWarehouse')
        granary = soup.find(id='stockBarGranary')
        storage['l1'] = int(warehouse.text.strip().replace(',', '')) if warehouse else 0
        storage['l2'] = int(warehouse.text.strip().replace(',', '')) if warehouse else 0
        storage['l3'] = int(warehouse.text.strip().replace(',', '')) if warehouse else 0
        storage['l4'] = int(granary.text.strip().replace(',', '')) if granary else 0
        data['storage'] = storage

        return data

    def _parse_dorf2_html(self, html_content):
        soup = BeautifulSoup(html_content, 'html.parser')
        buildings = []
        # City buildings are slots 19-40
        for i in range(19, 41):
            slot = soup.find('div', {'data-aid': str(i)})
            if slot:
                level_tag = slot.find('div', class_='labelLayer')
                level = int(level_tag.text) if level_tag else 0
                gid = int(slot.get('data-gid')) if slot.get('data-gid') else 0
                buildings.append({'id': i, 'level': level, 'gid': gid})
        return buildings
        
    async def get_prerequisites(self, village_id, build_id):
        session = await self._get_session()
        url = f"{self.base_url}/build.php?id={build_id}"
        async with session.get(url) as response:
            content = await response.text()
            soup = BeautifulSoup(content, 'html.parser')
            contract = soup.find('div', {'id': 'contract'})
            if not contract:
                return []
            
            prereqs = []
            lis = contract.find_all('li', class_='error')
            for li in lis:
                text = li.text
                if 'required' in text:
                    # E.g., "Main Building level 5 required"
                    parts = text.split(' level ')
                    name = parts[0].strip()
                    level = int(parts[1].split(' ')[0])
                    # Need to map name to GID
                    gid = self.name_to_gid(name)
                    if gid:
                        prereqs.append({'gid': gid, 'level': level, 'name': name})
            return prereqs

    def name_to_gid(self, name):
        gid_map = {
            "Woodcutter": 1, "Clay Pit": 2, "Iron Mine": 3, "Cropland": 4,
            "Sawmill": 5, "Brickyard": 6, "Iron Foundry": 7, "Grain Mill": 8,
            "Bakery": 9, "Warehouse": 10, "Granary": 11, "Smithy": 13,
            "Tournament Square": 14, "Main Building": 15, "Rally Point": 16,
            "Marketplace": 17, "Embassy": 18, "Barracks": 19, "Stable": 20,
            "Workshop": 21, "Academy": 22, "Cranny": 23, "Town Hall": 24,
            "Residence": 25, "Palace": 26, "Treasury": 27, "Trade Office": 28,
            "Great Barracks": 29, "Great Stable": 30, "City Wall": 31, "Earth Wall": 32,
            "Palisade": 33, "Stonemason's Lodge": 34, "Brewery": 35, "Trapper": 36,
            "Hero's Mansion": 37, "Great Warehouse": 38, "Great Granary": 39,
            "Wonder of the World": 40, "Horse Drinking Trough": 41, "Stone Wall": 42,
            "Makeshift Wall": 43, "Command Center": 44, "Waterworks": 45, "Hospital": 46
        }
        return gid_map.get(name)

    async def initiate_build(self, village_id, build_id, gid):
        session = await self._get_session()
        # Step 1: GET the build page to find the 'c' parameter (checksum)
        build_url = f"{self.base_url}/build.php?id={build_id}&gid={gid}"
        try:
            async with session.get(build_url) as response:
                content = await response.text()
                soup = BeautifulSoup(content, 'html.parser')
                build_button = soup.find('button', {'class': 'green build'})
                if not build_button:
                    log.warning(f"Could not find build button for GID {gid} at location {build_id}.")
                    return False
                
                onclick_attr = build_button.get('onclick', '')
                c_param_match = re.search(r"c=([a-f0-9]+)", onclick_attr)
                if not c_param_match:
                    log.warning(f"Could not find 'c' parameter for build GID {gid} at {build_id}.")
                    return False
                c_param = c_param_match.group(1)

            # Step 2: POST to dorf2.php to initiate the build
            post_url = f"{self.base_url}/dorf2.php?newdid={village_id}"
            params = {'a': gid, 'id': build_id, 'c': c_param}
            async with session.get(post_url, params=params) as response:
                if response.status == 200:
                    log.info(f"Successfully initiated build for GID {gid} at location {build_id}.")
                    return True
                else:
                    log.error(f"Failed to initiate build. Status: {response.status}")
                    return False
        except Exception as e:
            log.error(f"Exception during build initiation: {e}")
            return False

    async def close_session(self):
        if self.session and not self.session.closed:
            await self.session.close()