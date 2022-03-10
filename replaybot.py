# replaybot.py
import os, tempfile, json, sys, datetime, asyncio, hashlib
import requests

#async Http
import aiohttp

#some of these may be superfluous since I changed Google spreadsheet flow
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
import gspread

import discord
from dotenv import load_dotenv

DEBUG = True

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
WG_API_KEY = os.getenv('WG_TOKEN')
REPLAYSUNPACK_PATH = os.getenv('REPLAYSUNPACK_PATH')
NUM_CLANS = int(os.getenv('NUM_CLANS'))

CLANSHEETS = dict()
for i in range(1,NUM_CLANS+1):
	CLANSHEETS[os.getenv(f"CLAN{i}")] = os.getenv(f"SHEET{i}")

if DEBUG:
	print(CLANSHEETS)

#a bad hack for package problems
sys.path.insert(0, REPLAYSUNPACK_PATH)
from replays_unpack.replay_unpack.clients import wows
from replays_unpack.replay_unpack.replay_reader import ReplayReader, ReplayInfo


#TODO Non-NA URIs
NA_WOWS_URI = "https://api.worldofwarships.com/wows/"
WOWS_ACCOUNT_URI = f"https://api.worldofwarships.com/wows/account/statsbydate/?application_id={WG_API_KEY}&account_id="
WOWS_SHIP_URI = f"https://api.worldofwarships.com/wows/encyclopedia/ships/?application_id={WG_API_KEY}&ship_id="
WOWS_MAPS_URI = f"https://api.worldofwarships.com/wows/encyclopedia/battlearenas/?application_id={WG_API_KEY}"

client = discord.Client()

WIN_TYPES = { 1: "Kills", 9: "Time", 12:"Zeroed", 13:"Points" }

SHIPNAME_CACHE = dict()

#TODO: Upgrade to asyncio http requests
def get_ship_name(shipid):

	global SHIPNAME_CACHE

	if shipid not in SHIPNAME_CACHE:

		r = requests.get(WOWS_SHIP_URI + str(shipid))
		if r.status_code > 299 or r.status_code < 200:
			print(f'Failed to fetch ship data {shipid} with error code: {r.status_code}')
			return ""

		res = json.loads(r.content)
		if res["status"] != "ok":
			print(f'Failed to fetch ship data {shipid} with error code: {res["status"]}')
			return ""
			
		data = res['data'].popitem()[1]
		if data is None:
			print(f'Failed to fetch ship data {shipid}')
			return ""
		
		SHIPNAME_CACHE[shipid] = data["name"]
		
	return SHIPNAME_CACHE[shipid]

MAPDATA_CACHE = ""

def get_map_name(map_id):

	global MAPDATA_CACHE

	if MAPDATA_CACHE is "":
	
		r = requests.get(WOWS_MAPS_URI)
		if r.status_code > 299 or r.status_code < 200:
			print(f'Failed to fetch map data with error code: {r.status_code}')
			return ""

		res = json.loads(r.content)
		if res["status"] != "ok":
			print(f'Failed to fetch map data with error code: {res["status"]}')
			return ""
			
		data = res['data']
		#print(data)
		if data is None:
			print(f'Failed to fetch map data')
			return ""
		
		MAPDATA_CACHE = data
	
	return MAPDATA_CACHE[str(map_id)]["name"]
	
		
#TODO: Non-NA clans
async def calc_average_wr(team):

	teamWR = 0.0
	teamCount = 0
	
	async with aiohttp.ClientSession() as session:
		
		for player in team:
			async with session.get(WOWS_ACCOUNT_URI+str(player['accountDBID'])) as r:
				if r.status > 299 or r.status < 200:
					print(f'Failed to fetch player data {player["accountDBID"]} with error code: {r.status}')
					continue
			
				res = json.loads(await r.text())
				if res["status"] != "ok" or ( res['meta']['hidden'] ):
					print(f'Failed to fetch player data {player["accountDBID"]} with error code: {res["status"]}')
					continue		

				#print(player['accountDBID'])
				data = res['data'].popitem()[1]
				#print(data)
				if data['pvp'] is not None:
					pdata = data['pvp'].popitem()[1]
					teamWR = teamWR + (pdata['wins'] / pdata['battles'])

					teamCount = teamCount+1
				
	
	if teamCount == 0:
		return 0.0
		
	return teamWR/teamCount*100.0


#could optimize for current season, but left as a general function
def guess_clan(team):

	clans = dict()
	
	for player in team:
		clan = player['clanTag']
		
		if clan in clans:
			clans[clan] = clans[clan]+1
		else:
			clans[clan] = 1

	#probable a better way of doing this
	for clan in clans.keys():
		if clans[clan] >= (len(team)/2.0):
			return clan

	return ""

#gets the clan of the replay submitter
def get_player_clan(engine_data, hiddenData):
	for player in iter(hiddenData['players'].values()):
		if player["name"] == engine_data["playerName"]:
			return player["clanTag"]
			
	return ""


async def do_google_sheet(channel, teamA, teamB, teamAwr, teamBwr, winner, engine_data, hiddenData):
	global creds
	
	if not creds.valid:
		build_google_creds()
	
	
	gc = gspread.authorize(creds)

	#TeamA should always be the submitter's team. Probably.
	#too lazy to refactor
	"""
	ourClan = teamA
	theirClan = teamB
	clan = guess_clan(teamA)
	"""
	
	clan = get_player_clan(engine_data, hiddenData)
	
	if guess_clan(teamA) == clan:
		ourClan = teamA
		theirClan = teamB
	elif guess_clan(teamB) == clan:
		ourClan = teamB
		theirClan = teamA
	else:
		await send(channel, f"Not a supported clan: {clan} not {CLANSHEETS.keys()}. Mercs cannot submit replays!")
		return False
	

	if clan not in CLANSHEETS:
		await send(channel, f"Not a supported clan: {clan}")
		return False
		

	wbk = gc.open_by_key(CLANSHEETS[clan])
	sheet = wbk.get_worksheet(0)
	
	battleHash = hash_battle(engine_data, hiddenData)
	
	hashes = sheet.col_values(32)
	
	if battleHash in hashes:
		await send(channel, f"Battle already in sheet: {battleHash}")
		return False
	
	values = []

		
	clientDate = datetime.datetime.strptime(engine_data["dateTime"], "%d.%m.%Y %H:%M:%S")
	values.append(f'{clientDate:%Y-%m-%d %H:%M}')
		
	values.append(engine_data["playerName"])
	
	if winner[0:4] == "Team":
		winner = winner[8:-1]
	elif winner[0] == "[":
		winner = winner[1:-1]
	
	if winner == clan:
		values.append("Win")
	elif winner == "Draw":
		values.append("Draw")
	else:
		values.append("Loss")
	
	values.append(engine_data["weatherParams"]["0"][0])	
	values.append(get_map_name(engine_data["mapId"]))
	values.append(theirClan[0]["realm"])
	values.append(guess_clan(theirClan))
	
	for player in ourClan:
		values.append(player["name"])
		
	for player in ourClan:
		values.append(get_ship_name(player["shipParamsId"]))
		
	for player in theirClan:
		values.append(get_ship_name(player["shipParamsId"]))
	
	if clan == guess_clan(teamA):
		values.append(f'{teamAwr:.2f}')
		values.append(f'{teamBwr:.2f}')
	else:
		values.append(f'{teamBwr:.2f}')
		values.append(f'{teamAwr:.2f}')

	
	#damage reports are not accurate, so disabled
	"""
	for player in ourClan:
		values.append(f'{get_dmg_rcvd(player, hiddenData["shots_damage_map"]):.0f}')
	for player in theirClan:
		values.append(f'{get_dmg_rcvd(player, hiddenData["shots_damage_map"]):.0f}')

	for player in ourClan:
		values.append(f'{get_player_dmg(player, hiddenData["shots_damage_map"]):.0f}')	
	for player in theirClan:
		values.append(f'{get_player_dmg(player, hiddenData["shots_damage_map"]):.0f}')
	"""

	if hiddenData["battle_result"]["victory_type"] in WIN_TYPES:
		values.append(WIN_TYPES[hiddenData["battle_result"]["victory_type"]])
	else:
		values.append(f'Unknown: {hiddenData["battle_result"]["victory_type"]}')
	
	values.append(hash_battle(engine_data, hiddenData))
	await send(channel, f"Battle hash: {values[-1]}")
	
	sheet.append_row(values)
	
	return True
	


#helper function for sorting team lists
def by_player_id(item):
	return item["accountDBID"]


async def send(channel, msg):
	if DEBUG:
		now = datetime.datetime.now()
		current_time = now.strftime("%H:%M:%S")
		print("[",current_time, "]: ", msg)
	if channel:
		await channel.send(msg)


#try to uniquely identify a battle. A lot of irritating conversions simply because I can't pass strings directly to hash.update()!
def hash_battle(engine_data, hiddenData):

	hash = hashlib.sha256()
	
	hash.update(bytes(str(engine_data["mapId"]), 'utf-8'))
	hash.update(bytes(str(engine_data["clientVersionFromXml"]), 'utf-8'))
	hash.update(bytes(str(engine_data["weatherParams"]), 'utf-8'))
	hash.update(bytes(str(engine_data["duration"]), 'utf-8'))
	hash.update(bytes(str(engine_data["gameLogic"]), 'utf-8'))

	(teamA, teamB) = get_teams(hiddenData)
	
	#make sure they're in consistent order
	if teamA[0]["clanTag"] > teamB[0]["clanTag"]:
		tempTeam = teamA
		teamA = teamB
		teamB = tempTeam
	
	for player in teamA:
		hash.update(bytes(str(player["accountDBID"]), 'utf-8'))
		hash.update(bytes(str(player["fragsCount"]), 'utf-8'))
		hash.update(bytes(str(player["maxHealth"]), 'utf-8'))
		hash.update(bytes(str(player["shipParamsId"]), 'utf-8'))
		hash.update(bytes(str(player["skinId"]), 'utf-8'))
		
	for player in teamB:
		hash.update(bytes(str(player["accountDBID"]), 'utf-8'))
		hash.update(bytes(str(player["fragsCount"]), 'utf-8'))
		hash.update(bytes(str(player["maxHealth"]), 'utf-8'))
		hash.update(bytes(str(player["shipParamsId"]), 'utf-8'))
		hash.update(bytes(str(player["skinId"]), 'utf-8'))
		
	#might be backwards if the replay is from the other side
	hash.update(bytes(str(hiddenData["battle_result"]), 'utf-8'))
	
	#this might actually be all I need to uniquely ID a battle, but I have no way of confirming this
	#hash.update(bytes(hiddenData["arena_id"], 'utf-8'))
	
	#not confident that this would be consistent across replays
	#hash.update(bytes(hiddenData["death_info"], 'utf-8'))
	
	return hash.hexdigest()

#only works for *observed* damage
def get_dmg_rcvd(player, dmgMap):
	totDmg = 0.0
	
	if player["shipId"] not in dmgMap:
		return 0.0
	
	for dmg in dmgMap[player["shipId"]].values():
		totDmg = totDmg + dmg 
		
	return totDmg
		
	
def get_player_dmg(player, dmgMap):
	totDmg = 0.0
	
	for ship in dmgMap.keys():
		if player["shipId"] in dmgMap[ship]:
			totDmg = totDmg + dmgMap[ship[player["shipId"]]]
		
	return totDmg	

def get_teams(hiddenData):
	teamA = []
	teamB = []
	
	#sort players onto teams
	for player in iter(hiddenData['players'].values()):
		if player["teamId"] == 1:
			teamB.append(player)
		else:
			teamA.append(player)
			
	teamA.sort(key=by_player_id)
	teamB.sort(key=by_player_id)
	
	return (teamA, teamB)
	

#untested. Not even sure why I wrote this
def compare_teams(teamA, teamB):

	#in case they aren't already sorted
	teamA.sort(key=by_player_id)
	teamB.sort(key=by_player_id)
	
	#basically compare by shipId and account ID
	for (player1, player2) in zip(teamA, teamB):
		if player1['accountDBID'] != player2['accountDBID'] or player1['shipId'] != player2['shipId']:
			return False
			
	return True
		


async def analyze_replay(message, replay):
	reader = ReplayReader(replay)
	
	channel=None
	if message:
		channel = message.channel
	
	#parse the replay metadata with external library
	try:
		replay = reader.get_replay_data()
	except:
		await send(channel, 'Not a valid wowsreplay file')
		return
	
	#magic
	replayer = wows.ReplayPlayer(replay.engine_data
	   .get('clientVersionFromXml')
	   .replace(' ', '')
	   .split(','))
	   
	if replay.engine_data["matchGroup"] != "clan": # or not (replay.engine_data["matchGroup"] == "pvp" and replay.engine_data["playersPerTeam"] == 7 and replay.engine_data["gameLogic"] == "Domination" ):
		await send(channel, f'Not an accepted battle format: {replay.engine_data["matchGroup"]}')
		return 
	
	#run the replay. Essentially simulates complete battle	
	replayer.play(replay.decrypted_data, False)
	
	hiddenData = replayer.get_info()
	
	if hiddenData is None:
		await send(channel, "Invalid replay!")
		return
	
	map = get_map_name(replay.engine_data["mapId"])
	
	teamApretty = []
	teamBpretty = []
	
	(teamA, teamB) = get_teams(hiddenData)
	
	
	for player in teamA:
		teamApretty.append( f"[{player['clanTag']}] {player['name']}, {get_ship_name(player['shipParamsId'])} " )
	for player in teamB:
		teamBpretty.append( f"[{player['clanTag']}] {player['name']}, {get_ship_name(player['shipParamsId'])} " )
	
	
	if replay.engine_data["matchGroup"] == "clan":
		teamAwr = await calc_average_wr(teamA)
		teamBwr = await calc_average_wr(teamB)

	#determine who won
	winner = "Draw" #hey, it could happen
	if hiddenData["battle_result"]["winner_team_id"] == 1:
		winner = f"Team B [{guess_clan(teamB)}]"
	elif hiddenData["battle_result"]["winner_team_id"] == 0:
		winner = f"Team A [{guess_clan(teamA)}]"
		
	win_type = f'Unknown - tell rexstuff!: {hiddenData["battle_result"]["victory_type"]}'
	if hiddenData["battle_result"]["victory_type"] in WIN_TYPES:
		win_type = WIN_TYPES[hiddenData["battle_result"]["victory_type"]]
		
	clientDate = datetime.datetime.strptime(replay.engine_data["dateTime"], "%d.%m.%Y %H:%M:%S")

	"""
	msg = ( f'Analysis of battle submitted by {"" if not message else message.author}\nBattle time: {clientDate:%Y-%m-%d %H:%M}\nMap: {map}\n'
			f'Team A [{guess_clan(teamA)}]: {teamApretty}\n Team A avg WR: {teamAwr:.2f}\n'
			f'Team B [{guess_clan(teamB)}]: {teamBpretty}\n TeamB avg WR: {teamBwr:.2f}\n'
			f'Winner: {winner}, won by {win_type}' )
	"""
	
	msg = ( f'Analysis of battle submitted by {"" if not message else message.author}\nBattle time: {clientDate:%Y-%m-%d %H:%M}\nMap: {map}\n'
			f'Team A [{guess_clan(teamA)}], avg WR: {teamAwr:.2f}\n'
			f'Team B [{guess_clan(teamB)}], avg WR: {teamBwr:.2f}\n'
			f'Winner: {winner}, won by {win_type}' )

	
	if channel:
		await channel.send(msg)
	
	result = await do_google_sheet(channel, teamA, teamB, teamAwr, teamBwr, winner, replay.engine_data, hiddenData)
	
	if result:
		await send(channel, "Analysis completed successfully and Google sheet updated")
	else:
		await send(channel, "Analysis completed, but sheet not updated")
	
	

#download replay from discord	
async def get_replay(channel, tempdir, attachment):

	now = datetime.datetime.now()
	current_time = now.strftime("%H:%M:%S")

	#ignore MP4s from new bot
	if attachment.filename[-3:] == ".mp4"[-3:]:
		return None
	

	if attachment.filename[-11:] != ".wowsreplay"[-11:]:
		print(f'[{current_time}]: Not a WoWs replay:{attachment.filename}')
		await channel.send(f'Not a WoWs replay:{attachment.filename}')
		return None
		
	if attachment.size < 1:
		print(f'[{current_time}]: Empty file')
		await channel.send('Empty file')
		return None
		
	#protect myself. No replay should be bigger than 5MB
	if attachment.size > 5000000:
		print(f'[{current_time}]: File too large: {attachment.size}')
		await channel.send(f'File too large: {attachment.size}')
		return None
		
	#download from attachment.url
	r = requests.get(attachment.url)
	if r.status_code > 299 or r.status_code < 200:
		print(f'[{current_time}]: Failed to fetch file from {attachment.url} with error code: {r.status_code}')
		await channel.send(f'Failed to fetch file from {attachment.url} with error code: {r.status_code}')
		return None

	#write file to tempdir. Tempfiles won't work with replay_unpack
	filename = tempdir.name + "/" + attachment.filename
	file = open(filename, "wb")
	file.write(r.content)
	file.close()
	
	return filename
	
	
@client.event
async def on_ready():
	now = datetime.datetime.now()
	current_time = now.strftime("%H:%M:%S")
	
	print(f'[{current_time}]: {client.user} has connected to Discord!')
	print(f'Guilds: {client.guilds}')

@client.event
async def on_guild_join(Guild):
	print(f'{client.user} has joined to {Guild.name}!')


replayQueue = []

@client.event
async def on_message(message):
	now = datetime.datetime.now()
	current_time = now.strftime("%H:%M:%S")
	
	global replayQueue
	
	#ignore messages from self
	if message.author == client.user:
		return
		
	if message.content == "!up":
		await message.channel.send("Yup! Hit me. Send me your joosey repl4ys...")
		
	#TODO: Move channel names to .env
	if message.content == "!analyze" or ( (message.channel.name == "secret-stat-stuff" or message.channel.name == "cb-replay-dump" or message.channel.name == "nom-replay-analysis" or message.channel.name == "replay-central" or message.channel.name == "yolos-replay-dump" or message.channel.name == "yolo-replay-central") and (message.attachments is not None and (len(message.attachments) > 0) )):
		print(f'[{current_time}]: Message analysis received!')
	
		if message.content == "!analyze" and (message.attachments is None or (len(message.attachments) < 1)):
			print(f'[{current_time}]: No attachment. Attach a WoWs CB replay to have it analyzed!')
			await message.channel.send("No attachment")
			return
			
		tempdir = tempfile.TemporaryDirectory()
			
		for attach in message.attachments:
		
			filename = await get_replay(message.channel, tempdir, attach)
			
			if filename is not None:
				await message.channel.send(f"File {attach.filename} downloaded, beginning analysis, please wait...")
				await analyze_replay(message, filename)
		
		tempdir.cleanup()
				
			


def build_google_creds():

	#SCOPES = ['https://spreadsheets.google.com/feeds']
	SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
	creds = None
	# The file token.json stores the user's access and refresh tokens, and is
	# created automatically when the authorization flow completes for the first
	# time.
	if os.path.exists('token.json'):
		creds = Credentials.from_authorized_user_file('token.json', SCOPES)
	# If there are no (valid) credentials available, let the user log in.
	if not creds or not creds.valid:
		if creds and creds.expired and creds.refresh_token:
			creds.refresh(Request())
		else:
			flow = InstalledAppFlow.from_client_secrets_file(
				'credentials.json', SCOPES)
			creds = flow.run_local_server(port=0)
		# Save the credentials for the next run
		with open('token.json', 'w') as token:
			token.write(creds.to_json())
			
	return creds
	

creds = build_google_creds()


async def test():
	await analyze_replay(None, "20210310_222813_PRSC109-Dmitry-Donskoy_16_OC_bees_to_honey.wowsreplay")
	#await analyze_replay(None, "20210406_215642_PASD505-Hill_33_new_tierra.wowsreplay")
	#await analyze_replay(None, "20210406_221346_PGSD105-T-22_41_Conquest.wowsreplay")
	
client.run(TOKEN)

#asyncio.run( test() )



