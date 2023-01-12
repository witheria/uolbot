import asyncio
import json
import logging
import os
import sys
from contextlib import suppress
from typing import Callable, Union
from logging.handlers import TimedRotatingFileHandler

import aiohttp
import interactions
from dotenv import load_dotenv
from tabulate import tabulate

_file_handler = TimedRotatingFileHandler("./logs", when="midnight", interval=1, backupCount=7)
_console_handler = logging.StreamHandler(sys.stdout)
logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _console_handler])
load_dotenv()


dc_key = os.getenv("DISCORD_API_KEY")
rito_key = os.getenv("RIOT_API_KEY")

queue_types = ["RANKED_SOLO_5x5", "RANKED_FLEX_SR", "RANKED_TFT_DOUBLE_UP"]
queue_types_names = {"RANKED_SOLO_5x5": "Ranked Solo/Duo", "RANKED_FLEX_SR": "Ranked Flex", "RANKED_TFT_DOUBLE_UP": "TFT Double Up"}


_bot: interactions.Client = interactions.Client(dc_key, logging=logging.INFO)


# Mode select (Component Callback)
mode_select = interactions.SelectMenu(
        options=[
            interactions.SelectOption(label="Solo Queue", value="RANKED_SOLO_5x5"),
            interactions.SelectOption(label="Flex Queue", value="RANKED_FLEX_SR"),
            interactions.SelectOption(label="TFT Double Up", value="RANKED_TFT_DOUBLE_UP"),
        ],
        placeholder="Select a gamemode...",
        custom_id="mode_select"
)


class HeartbeatAsyncio:
    """
    Class to repeatedly call an async function

    Constructor takes:
        func: A callable function to be executed after a set interval

        interval: The interval in which the callable should be executed
    """

    func: Callable
    time: int
    is_started: bool
    _task: Union[asyncio.Task, None]

    def __init__(self, func: callable, interval: int):
        self.func = func
        self.time = interval
        self.is_started = False
        self._task = None

    async def start(self):
        if not self.is_started:
            self.is_started = True
            # Start task to call func periodically:
            self._task = asyncio.ensure_future(self._run())

    async def stop(self):
        if self.is_started:
            self.is_started = False
            # Stop task and await it stopped:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    async def _run(self):
        while True:
            await asyncio.sleep(self.time)
            try:
                await self.func()
            except Exception as e:
                logging.warning(e, exc_info=True)


class Tiax:

    """
    Session that is created once per server. 

    Contains all necessary information for a server and is changed/saved/loaded
    separately from the main bot (Im too poor to connect it to a database, sry).
    """

    _session: aiohttp.ClientSession
    _call_loop: HeartbeatAsyncio

    summ_data: dict = {
    }

    last_ranks: dict = {}
    
    guild: str = None

    send_updates: bool = False
    update_interval = 3600
    updates_channel: str = None
    show_unranked_players: bool = False

    # Constants
    by_name = "https://euw1.api.riotgames.com/lol/summoner/v4/summoners/by-name/{0}"  # summ name
    by_summoner = "https://euw1.api.riotgames.com/lol/league/v4/entries/by-summoner/{0}"  # enc summ id

    def __init__(self, guild: str):
        self.guild = guild
        try:
            self.load_from_file()
        except Exception as e:
            logging.error("Could not load from file: %s", e, exc_info=True)
        asyncio.ensure_future(self._create_Session())
        self._call_loop = HeartbeatAsyncio(self.getSummoners, self.update_interval)
        asyncio.ensure_future(self._call_loop.start())

    async def _create_Session(self):
        """Creates the main aiohttp Session"""
        self._session = aiohttp.ClientSession()

    async def _bySummoner(self, name: str):
        """Send a request to the riot api to get the current ranks of a player"""
        return await self._fetch_riot(self.by_summoner.format(self.summ_data[name]["data"]["id"]))

    async def _byName(self, name: str):
        """Sends a request to the riot api to get the summoner data"""
        return await self._fetch_riot(self.by_name.format("%20".join(name.split())))

    async def _fetch_riot(self, url):
        """Main request executor, sends a request over the main session with basic headers"""
        headers = {
            "User-Agent"     : "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:107.0) Gecko/20100101 Firefox/107.0",
            "Accept-Language": "de,en-US;q=0.7,en;q=0.3",
            "Accept-Charset" : "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin"         : "https://developer.riotgames.com",
            "X-Riot-Token"   : rito_key
        }
        async with self._session.get(url, headers=headers) as s:
            if s.status not in range(200, 300):
                logging.error("request to url %s failed!", url)
                return False
            return await s.json()
    
    async def change_interval(self, interval: int):
        """Changes the timer interval interactively"""
        await self._call_loop.stop()
        self._call_loop.time = interval
        await self._call_loop.start()


    def save_to_file(self):
        """Saves this Tiax's settings to a file"""
        with open(self.guild + "-accounts.json", "w+", encoding="utf-8") as file:
            dump = self.summ_data
            dump["config"] = {
                "last_ranks": self.last_ranks, 
                "show_unrankeds": self.show_unranked_players,
                "send_updates": self.send_updates,
                "update_interval": self.update_interval,
                "updates_channel": self.updates_channel}
            file.write(json.dumps(dump, indent=4, ensure_ascii=False))

    def load_from_file(self):
        """Loads this Tiax's settings from a file"""
        if not os.path.exists(self.guild + "-accounts.json"):
            return
        with open(self.guild + "-accounts.json", "r", encoding="utf-8") as file:
            data: dict = json.loads(file.read())
            if data:
                if "config" in data.keys():
                    default = {"show_unrankeds": False, "last_ranks": {}, "send_updates": True}
                    conf = data.pop("config", default)
                    self.show_unranked_players = conf["show_unrankeds"]
                    self.last_ranks = conf["last_ranks"]
                    self.send_updates = conf["send_updates"]
                    self.updates_channel = conf["updates_channel"]
                    self.update_interval = conf["update_interval"]
                self.summ_data = data

    async def getSummoners(self) -> tuple:
        """
        Main function to retrieve summoner data and post it into the channels
        
        This function gets called in intervals if send_updates is True
        """
        errs = []
        for name in self.summ_data.keys():
            print(name)
            result1 = await self._byName(name)
            print(result1)
            if result1:
                self.summ_data[name]["data"] = result1
                await asyncio.sleep(1)
                result2 = await self._bySummoner(name)
                self.summ_data[name]["info"] = result2
                await asyncio.sleep(0.1)
            else:
                errs.append(name)
        for name in errs:
            self.summ_data.pop(name)
        self.save_to_file()
        if self.send_updates and self.updates_channel:
            for guild in _bot.guilds:
                if str(guild.id) == self.guild:
                    for channel in guild.channels:
                        if str(channel.id) == self.updates_channel:
                            await channel.send(embeds=self.generateLeaderboardEmbed(), components=mode_select)
        
        return len(errs) < 1, errs

    def getSummoner(self, name: str):
        """Gets a summoners main data"""
        if self._main_loop:
            data = self._main_loop.create_task(self._bySummoner(name))
            if data:
                self.summ_data[name]["data"] = data
                self.save_to_file()
            else:
                logging.error("Summoner %s could not be loaded", name)

    def rito_pls(self, name: str):
        """useless"""
        if not self._main_loop:
            return
        if name not in self.summ_data.keys():
            self.getSummoner(name)
        if name not in self.summ_data.keys():
            logging.error("Name not found")
            return
        return self._main_loop.create_task(self._bySummoner(name))

    def generateLeaderboardEmbed(self, gamemodes: list = None, modcall: bool = False,) -> interactions.Embed:

        """Generates the main leaderboard embed."""

        if gamemodes is None:
            gamemodes = ["RANKED_SOLO_5x5"]

        good_data = sortByTier(self.summ_data, show_unrankeds=self.show_unranked_players)
        for gamemode in gamemodes:
            embed = interactions.Embed(title=f"Leaderboard {queue_types_names[gamemode]}", name="Leaderboard")
            tab_headers = ["Spot", "Name", "Rang", "Wins", "Losses"]
            to_table = [
                [good_data[result][x] for x in range(len(good_data[result])) if x not in range(1, 3)]
                for result in range(len(good_data)) if good_data[result][1] == gamemode]
            for li in range(len(to_table)):
                to_table[li].insert(0, str(li + 1))
            
            ranking = [[y for y in x] for x in to_table]
            if not modcall:
                if gamemode in self.last_ranks.keys():
                    for li in range(len(self.last_ranks[gamemode])):
                        for yi in range(len(to_table)):
                            text = ""
                            if to_table[yi][1] == self.last_ranks[gamemode][li][1]:
                                if int(to_table[yi][0]) < int(self.last_ranks[gamemode][li][0]):
                                    text += f"▲{li - yi} "
                                if int(to_table[yi][0]) > int(self.last_ranks[gamemode][li][0]):
                                    text += f"▼{yi - li} "
                                ranking[yi][0] = text + f"{yi}"
                                break
            logging.info("Last Ranks: %s", self.last_ranks if self.last_ranks else "")
            logging.info(ranking)
            self.last_ranks[gamemode] = to_table
            embed.add_field("⠀", "```\n" + tabulate(ranking, headers=tab_headers) + "```")
            return embed


class Config:

    """Sussy class"""

    _save = "savege.json"

    guilds = {}

    def __init__(self) -> None:
        self.load()

    def save(self) -> None:
        with open(self._save, "w+", encoding="utf-8") as file:
            file.write(json.dumps(list(i for i in self.guilds.keys()), skipkeys=True, ensure_ascii=False, indent=4))

    def load(self) -> None:
        if not os.path.isfile(self._save):
            self.guilds = {}
            return
        with open(self._save, "r", encoding="utf-8") as file:
            self.guilds = {i: Tiax(str(i)) for i in json.loads(file.read())}


# Create the main sussy class
c = Config()
# Create all tiaxes, in case they havent been loaded already with sussy.load
c.guilds = {str(guild): Tiax(str(guild)) for guild in c.guilds.keys()}


def sortByTier(players: dict, show_unrankeds: bool = False):
    """I would advise against touching this"""

    tier = ['UNRANKED', 'IRON', 'BRONZE', 'SILVER', 'GOLD', 'PLATINUM', 'DIAMOND', 'MASTER', 'GRANDMASTER',
            'CHALLENGER']
    rank = ['IV', 'III', 'II', 'I']
    playings = []

    for player in players:
        if player == "config":  # The config for this class is not a player xd
            continue
        sub_queues = {}
        if "info" in players[player]:
            sub_queues: dict = nameDictByKeyValue(players[player]["info"], key="queueType")
        for _type in queue_types:
            if _type not in sub_queues.keys():
                sub_queues[_type] = {}
        print(sub_queues)

        for queue in sub_queues:
            if len(sub_queues[queue].keys()) > 0:
                try:
                    points = tier.index(sub_queues[queue]["tier"]) * 100000 + \
                            rank.index(sub_queues[queue]["rank"]) * 10000 + \
                            sub_queues[queue]["leaguePoints"]
                except ValueError as v:
                    logging.error("The tier/rank does not exist in the given tiers/ranks: %s", players[player]["info"][queue])
                    continue
                print(f"{player} internal skillevel for {queue} is {points}")
                playings.append((
                    players[player]["data"]["name"],                    # Name
                    sub_queues[queue]["queueType"],                     # Queue
                    points,                                             # Skillevel
                    str(sub_queues[queue]["tier"] + " " +               
                        sub_queues[queue]["rank"] + " " +
                        str(sub_queues[queue]["leaguePoints"]) + " LP"),  # Text
                    sub_queues[queue]["wins"],                          # wins
                    sub_queues[queue]["losses"],                         # losses
                    # int(sub_queues[queue]["wins"]) + int(sub_queues[queue]["losses"])  # games
                    ))

            elif show_unrankeds:
                print(player, "is currently unranked on this mode!")
                playings.append((players[player]["data"]["name"],
                                queue,
                                0,
                                str("Unranked"),
                                0,
                                0,
                                # 0
                                ))

    return sorted(playings, key=lambda x: x[2], reverse=True)


def nameDictByKeyValue(putin: list, key: str, deleteKey: bool = False) -> dict:
    """
    Put the value of a key as a key in front of the dictionary within a list

    Example [{1:"word", 2:"cringe"}, {4:"word", 2:"master"}, {"garden":"eden", 34:"word", 2:"woven"}] ->
    [{'cringe': {1: 'word', 2: 'cringe'}, 
    'master': {4: 'word', 2: 'master'},
    'woven': {'garden': 'eden', 34: 'word', 2: 'woven'}}]

    deletes the key from the dictionary if deleteKey is True

    (Even though it looks like I copied this, this is my creation)
    (Also, putin is not a reference to the actual president, its "input" reversed)
    """
    if not deleteKey:
        return {dictionary[key]: dictionary for dictionary in putin}
    return {dictionary.pop(key): dictionary for dictionary in putin}
    


@_bot.command(name="leaderboard")
async def leaderboard(ctx: interactions.CommandContext):
    """Send a leaderboard"""
    if str(ctx.guild_id) not in c.guilds.keys() and type(c.guilds[str(ctx.guild_id)]) != Tiax:
        c.guilds[str(ctx.guild_id)] = Tiax(str(ctx.guild_id))

    emb = c.guilds[str(ctx.guild_id)].generateLeaderboardEmbed()
    if not emb:
        return
    await ctx.send(embeds=emb, components=mode_select)


@_bot.command(name="refresh", default_member_permissions=interactions.Permissions.ADMINISTRATOR)
async def refresh(ctx: interactions.CommandContext):
    """Refresh it all (horrible function)"""
    await ctx.defer()

    if str(ctx.guild_id) not in c.guilds.keys() and type(c.guilds[str(ctx.guild_id)]) != Tiax:
        c.guilds[str(ctx.guild_id)] = Tiax(str(ctx.guild_id))

    msg = await ctx.send("Refreshing ranks...")
    await c.guilds[str(ctx.guild_id)].getSummoners()
    await msg.edit(f"Ranks refreshed successfully!")


@_bot.component("mode_select")
async def mode_selector(ctx: interactions.CommandContext, response: list):
    await ctx.edit(embeds=c.guilds[str(ctx.guild_id)].generateLeaderboardEmbed(gamemodes=response, modcall=True))


@_bot.command(
        name="add_player",
        description="Add a player to the leaderboard!",
        options=[
            interactions.Option(
                    name="player_name",
                    description="The Players summoner name (must be EUW)",
                    type=interactions.OptionType.STRING,
                    required=True,
            ),
        ],
)
async def add_player(ctx: interactions.CommandContext, player_name: str):
    """Adds a player to this guilds Tiax"""
    if str(ctx.guild_id) not in c.guilds.keys() and type(c.guilds[str(ctx.guild_id)]) != Tiax:
        c.guilds[str(ctx.guild_id)] = Tiax(str(ctx.guild_id))
    await ctx.defer()
    msg = await ctx.send("Adding Player...")
    c.guilds[str(ctx.guild_id)].summ_data[player_name] = {}
    await c.guilds[str(ctx.guild_id)].getSummoners()
    await msg.edit(f"The Summoner {player_name} was added successfully!")


@_bot.command(
    name="batch_add_players",
    description="Add a whole bunch of players at the same time!",
    options=[
        interactions.Option(
            name="players",
            description="Accepts json or separator-split (default: whitespace) formats",
            type=interactions.OptionType.STRING,
            required=True,
        ),
        interactions.Option(
            name="submit_type",
            description="What kind of format are you using?",
            type=interactions.OptionType.STRING,
            required=True,
            choices=[
                interactions.Choice(name="separator", value="sep"),
                interactions.Choice(name="json", value="json"),
            ],
        ),
        interactions.Option(
            name="separator",
            description="What custom separator are you using?",
            type=interactions.OptionType.STRING,
            required=False,
        )
    ]
)
async def batch_add_players(ctx: interactions.CommandContext, players: str, submit_type: str, separator: str = None):
    """this was also not very pleasant to write"""
    await ctx.defer()
    if str(ctx.guild_id) not in c.guilds.keys() and type(c.guilds[str(ctx.guild_id)]) != Tiax:
        c.guilds[str(ctx.guild_id)] = Tiax(str(ctx.guild_id))
    
    proxy_players = []
    if submit_type == "sep" and separator:
        try:
            proxy_players = players.split(sep=separator)
        except Exception as e:
            logging.error("The data could not be separated with custom separator: %s", e)
            await ctx.send("The data you send could not be decoded")
            return
    
    if submit_type == "sep":
        try:
            proxy_players = players.split()
        except Exception as e:
            logging.error("The data could not be separated with default whitespace: %s", e)
            await ctx.send("The data you send could not be decoded")
            return

    if submit_type == "json":
        try:
            proxy_players = json.loads(players)
        except Exception as e:
            logging.error("JSON could not be decoded! %s", e)
            await ctx.send("The data you send could not be decoded")
            return

    if len(proxy_players) < 1:
            logging.error("No players found!")
            await ctx.send("The data you send contains no player names!")
            return

    for ply in proxy_players:
        if ply not in c.guilds[str(ctx.guild_id)].summ_data.keys():
            c.guilds[str(ctx.guild_id)].summ_data[ply.strip()] = {}
    
    res = await c.guilds[str(ctx.guild_id)].getSummoners()

    if not res[0]:
        await ctx.send(f"The Player(s) {', '.join(res[1])} could not be loaded!")
        return

    await ctx.send("Players added successfully!")
    

@_bot.command(
        name="show_unranked_players",
        description="Show the unranked players on your leaderboard!",
        options=[
            interactions.Option(
                name="show_unranked_players",
                description="Whether to show unranked players on the leaderboard!",
                type=interactions.OptionType.BOOLEAN,
                required=False,
            ),
        ],
)
async def show_unranked_players(ctx: interactions.CommandContext, show_unranked_players: bool = None):
    """self explanatory by the command description i think"""
    if str(ctx.guild_id) not in c.guilds.keys() and type(c.guilds[str(ctx.guild_id)]) != Tiax:
        c.guilds[str(ctx.guild_id)] = Tiax(str(ctx.guild_id))
    
    if not show_unranked_players:
        await ctx.send(f"The leaderboard gets send {'with' if c.guilds[str(ctx.guild_id)].show_unranked_players else 'without'} unranked players")
        return
    c.guilds[str(ctx.guild_id)].show_unranked_players = show_unranked_players
    logging.info("Changed show_unranked_players for guild %s to %s", str(ctx.guild_id), show_unranked_players)
    c.guilds[str(ctx.guild_id)].save_to_file()
    await ctx.send("Successfully changed!")


@_bot.command(
        name="send_updates",
        description="Send updates to a specific channel!",
        options=[
            interactions.Option(
                name="send_updates",
                description="Whether to send updates, default is the channel this command was called in!",
                type=interactions.OptionType.BOOLEAN,
                required=False,
            ),
            interactions.Option(
                name="updates_channel",
                description="The channel to send updates in!",
                type=interactions.OptionType.CHANNEL,
                required=False
            ),
            interactions.Option(
                name="updates_interval",
                description="The interval (in seconds) in which updates should be sent. Default: 3600 (1 hour), Minimum: 900",
                type=interactions.OptionType.INTEGER,
                required=False
            )
        ],
)
async def send_updates(ctx: interactions.CommandContext, 
                        send_updates: bool = True, 
                        updates_channel: interactions.Channel = None, 
                        updates_interval: int = 3600):
    """Like the one before, but with more spice"""
    await ctx.defer()
    chn = None
    updates_interval = 900 if updates_interval < 900 else updates_interval
    if str(ctx.guild_id) not in c.guilds.keys() and type(c.guilds[str(ctx.guild_id)]) != Tiax:
        c.guilds[str(ctx.guild_id)] = Tiax(str(ctx.guild_id))

    if not send_updates and not updates_channel:
        await ctx.send(f"Currently updates {'are' if c.guilds[str(ctx.guild_id)].send_updates else 'are not'} getting sent!")
        return
    
    if not updates_channel:
        chn = ctx.channel_id
    else:
        chn = updates_channel.id
    
    if not chn:
        await ctx.send("Something went wrong while setting your channel!")
        return

    c.guilds[str(ctx.guild_id)].send_updates = send_updates
    c.guilds[str(ctx.guild_id)].updates_channel = str(chn)
    await c.guilds[str(ctx.guild_id)].change_interval(int(updates_interval))

    logging.info("Changed send_updates for guild %s to %s, channel: %s", str(ctx.guild_id), send_updates, chn)
    c.guilds[str(ctx.guild_id)].save_to_file()
    await ctx.send("Successfully changed!")


@_bot.command(
        name="remove_player",
        description="Remove a player from the leaderboard!",
        options=[
            interactions.Option(
                    name="player_name",
                    description="The Players summoner name (must be EUW and already on the leaderboard)",
                    type=interactions.OptionType.STRING,
                    required=True,
            ),
        ],
)
async def remove_player(ctx: interactions.CommandContext, player_name: str):
    """Removes a player from the list"""
    await ctx.defer()

    if str(ctx.guild_id) not in c.guilds.keys() and type(c.guilds[str(ctx.guild_id)]) != Tiax:
        c.guilds[str(ctx.guild_id)] = Tiax(str(ctx.guild_id))

    if player_name not in c.guilds[str(ctx.guild_id)].summ_data.keys():
        await ctx.send("This player is not saved in the list!")
        return

    try:
        c.guilds[str(ctx.guild_id)].summ_data.pop(player_name)
    except Exception as e:
        logging.error("Something went wrong! %s", e, exc_info=True)
        await ctx.send("Something went wrong!")
        return

    await ctx.send(f"{player_name} was successfully deleted from the leaderboard!")


@_bot.command(
        name="drop_leaderboard",
        description="Wipe the entire leaderboard clean",
        default_member_permissions=interactions.Permissions.ADMINISTRATOR
)
async def drop_leaderboard(ctx: interactions.CommandContext):
    if str(ctx.guild_id) not in c.guilds.keys() and type(c.guilds[str(ctx.guild_id)]) != Tiax:
        c.guilds[str(ctx.guild_id)] = Tiax(str(ctx.guild_id))

    c.guilds[str(ctx.guild_id)].summ_data = {}
    c.guilds[str(ctx.guild_id)].save_to_file()
    await ctx.send("Leaderboard dropped!")


@_bot.command(
        name="get_json",
        description="Get all Players as a JSON list",
        default_member_permissions=interactions.Permissions.ADMINISTRATOR
)
async def get_json(ctx: interactions.CommandContext):
    if str(ctx.guild_id) not in c.guilds.keys() and type(c.guilds[str(ctx.guild_id)]) != Tiax:
        c.guilds[str(ctx.guild_id)] = Tiax(str(ctx.guild_id))

    players = [i for i in c.guilds[str(ctx.guild_id)].summ_data.keys()]
    await ctx.send(json.dump(players, ensure_ascii=False, sort_keys=True))


@_bot.command(
    name="save",
    description="Save the current settings",
    default_scope=False,
    scope=778001302130130965
)
async def save(ctx: interactions.CommandContext):
    try:
        c.guilds = {key: c.guilds[key].updates_channel for key in c.guilds.keys()}
        c.save()
    except Exception as e:
        await ctx.send("Exception while saving!")
        return
    await ctx.send("Saved successfully!")


@_bot.command(
    name="load",
    description="Attempt to load from the file",
    default_scope=False,
    scope=778001302130130965
)
async def load(ctx: interactions.CommandContext):
    try:
        c.load()
    except Exception as e:
        await ctx.send("Exception while loading!")
        return
    await ctx.send("Loaded successfully!")


_bot.start()
for guild in c.guilds.keys():
    c.guilds[str(guild)].save_to_file()
c.save()