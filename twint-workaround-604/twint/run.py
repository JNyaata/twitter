import sys, os, time
from asyncio import get_event_loop, TimeoutError, ensure_future
from datetime import datetime

from . import datelock, feed, get, output, verbose, storage
from .storage import db
#from . import _logme
#
#logme = _logme._logger(__name__)

import logging as logme

class Twint:
    def __init__(self, config):
        logme.debug(__name__+':Twint:__init__')
        if config.Resume is not None and (config.TwitterSearch or config.Followers or config.Following):
            logme.debug(__name__+':Twint:__init__:Resume')
            self.init = self.get_resume(config.Resume)
        else:
            self.init = -1
            
        self.feed = [-1]
        self.count = 0
        self.consecutive_errors_count = 0
        self.has_more_items = True
        self._has_more_items = True
        self.user_agent = ""
        self.config = config
        self.conn = db.Conn(config.Database)
        self.d = datelock.Set(self.config.Until, self.config.Since)
        verbose.Elastic(config.Elasticsearch)

        if self.config.Store_object:
            logme.debug(__name__+':Twint:__init__:clean_follow_list')
            output.clean_follow_list()

        if self.config.Pandas_clean:
            logme.debug(__name__+':Twint:__init__:pandas_clean')
            storage.panda.clean()

    def get_resume(self, resumeFile):
        if not os.path.exists(resumeFile):
            return -1
        with open(resumeFile, 'r') as rFile:
            _init = rFile.readlines()[-1].strip('\n')
            return _init

    async def Feed(self):
        logme.debug(__name__+':Twint:Feed')
        
        response = await get.RequestUrl(self.config, self.init, headers=[("User-Agent", self.user_agent)])

        if self.config.Debug:
            print(response, file=open("twint-last-request.log", "w", encoding="utf-8"))
            print(f"had_more_items:{self._has_more_items};has_more_items:{self.has_more_items};init:{self.init};len_feed:{len(self.feed)}",
                file=open("twint-requests-deep.csv", 'a'))
        if self.config.Resume:
            print(self.init, file=open(self.config.Resume, "w", encoding="utf-8"))
            
        self.feed = []
        try:
            if self.config.Favorites:
                self.feed, self.init = feed.Mobile(response)
                if not self.count%40:
                    time.sleep(5)
            elif self.config.Followers or self.config.Following:
                self.feed, self.init = feed.Follow(response)
                if not self.count%40:
                    time.sleep(5)
            elif self.config.Profile:
                if self.config.Profile_full:
                    self.feed, self.init = feed.Mobile(response)
                else:
                    self.feed, self.init = feed.profile(response)
            elif self.config.TwitterSearch:
                self.feed, self.init, self._has_more_items = feed.Json(response)
                if (not self.feed) and self.has_more_items:
                    await self.Feed()
                self.has_more_items = self._has_more_items
            return
        except TimeoutError as e:
            if self.config.Proxy_host.lower() == "tor":
                print("[?] Timed out, changing Tor identity...")
                if self.config.Tor_control_password is None:
                    logme.critical(__name__+':Twint:Feed:tor-password')
                    sys.stderr.write("Error: config.Tor_control_password must be set for proxy autorotation!\r\n")
                    sys.stderr.write("Info: What is it? See https://stem.torproject.org/faq.html#can-i-interact-with-tors-controller-interface-directly\r\n")
                    exit(1)
                else:
                    get.ForceNewTorIdentity(self.config)
                    await self.Feed()
            else:
                logme.critical(__name__+':Twint:Feed:' + str(e))
                exit(str(e))
        except Exception as e:
            if self.config.Profile or self.config.Favorites:
                print("[!] Twitter does not return more data, scrape stops here.")
                return
            logme.critical(__name__+':Twint:Feed:noData' + str(e))
            # Sometimes Twitter says there is no data. But it's a lie.
            self.consecutive_errors_count += 1
            if self.consecutive_errors_count < self.config.Retries_count:
                self.user_agent = await get.RandomUserAgent(wa=True if self.config.TwitterSearch else False)
                time.sleep(5)
                await self.Feed()
            logme.critical(__name__+':Twint:Feed:Tweets_known_error:' + str(e))
            print(str(e) + " [x] run.Feed\n"+
                "[!] if get this error but you know for sure that more tweets exist, please open an issue and we will investigate it!")
            return
            

    async def follow(self):
        self.consecutive_errors_count = 0
        await self.Feed()
        if self.config.User_full:
            logme.debug(__name__+':Twint:follow:userFull')
            self.count += await get.Multi(self.feed, self.config, self.conn)
        else:
            logme.debug(__name__+':Twint:follow:notUserFull')
            for user in self.feed:
                self.count += 1
                username = user.find("a")["name"]
                await output.Username(username, self.config, self.conn)

    async def favorite(self):
        self.consecutive_errors_count = 0
        logme.debug(__name__+':Twint:favorite')
        await self.Feed()
        self.count += await get.Multi(self.feed, self.config, self.conn)

    async def profile(self):
        self.consecutive_errors_count = 0
        await self.Feed()
        if self.config.Profile_full:
            logme.debug(__name__+':Twint:profileFull')
            self.count += await get.Multi(self.feed, self.config, self.conn)
        else:
            logme.debug(__name__+':Twint:notProfileFull')
            for tweet in self.feed:
                self.count += 1
                await output.Tweets(tweet, self.config, self.conn)

    async def tweets(self):
        self.consecutive_errors_count = 0
        await self.Feed()
        if self.config.Location:
            logme.debug(__name__+':Twint:tweets:location')
            self.count += await get.Multi(self.feed, self.config, self.conn)
        else:
            logme.debug(__name__+':Twint:tweets:notLocation')
            for tweet in self.feed:
                self.count += 1
                await output.Tweets(tweet, self.config, self.conn)

    async def main(self, callback=None):

        task = ensure_future(self.run())  # Might be changed to create_task in 3.7+.

        if callback:
            task.add_done_callback(callback)

        await task

    async def run(self):
        if self.config.TwitterSearch:
            self.user_agent = await get.RandomUserAgent(wa=True)
        else:
            self.user_agent = await get.RandomUserAgent()
        if self.config.User_id is not None:
            logme.debug(__name__+':Twint:main:user_id')
            self.config.Username = await get.Username(self.config.User_id)

        if self.config.Username is not None:
            logme.debug(__name__+':Twint:main:username')
            url = f"https://twitter.com/{self.config.Username}?lang=en"
            self.config.User_id = await get.User(url, self.config, self.conn, True)

        if self.config.TwitterSearch and self.config.Since and self.config.Until:
            logme.debug(__name__+':Twint:main:search+since+until')
            while self.d._since < self.d._until:
                self.config.Since = str(self.d._since)
                self.config.Until = str(self.d._until)
                if len(self.feed) > 0:
                    await self.tweets()
                else:
                    logme.debug(__name__+':Twint:main:gettingNewTweets')
                    break

                if get.Limit(self.config.Limit, self.count):
                    break
        else:
            logme.debug(__name__+':Twint:main:not-search+since+until')
            while True:
                if len(self.feed) > 0:
                    if self.config.Followers or self.config.Following:
                        logme.debug(__name__+':Twint:main:follow')
                        await self.follow()
                    elif self.config.Favorites:
                        logme.debug(__name__+':Twint:main:favorites')
                        await self.favorite()
                    elif self.config.Profile:
                        logme.debug(__name__+':Twint:main:profile')
                        await self.profile()
                    elif self.config.TwitterSearch:
                        logme.debug(__name__+':Twint:main:twitter-search')
                        await self.tweets()
                else:
                    logme.debug(__name__+':Twint:main:no-more-tweets')
                    break

                #logging.info("[<] " + str(datetime.now()) + ':: run+Twint+main+CallingGetLimit2')
                if get.Limit(self.config.Limit, self.count):
                    logme.debug(__name__+':Twint:main:reachedLimit')
                    break

        if self.config.Count:
            verbose.Count(self.count, self.config)

def run(config, callback=None):
    logme.debug(__name__+':run')
    get_event_loop().run_until_complete(Twint(config).main(callback))

def Favorites(config):
    logme.debug(__name__+':Favorites')
    config.Favorites = True
    config.Following = False
    config.Followers = False
    config.Profile = False
    config.Profile_full = False
    config.TwitterSearch = False
    run(config)
    if config.Pandas_au:
        storage.panda._autoget("tweet")

def Followers(config):
    logme.debug(__name__+':Followers')
    output.clean_follow_list()
    config.Followers = True
    config.Following = False
    config.Profile = False
    config.Profile_full = False
    config.Favorites = False
    config.TwitterSearch = False
    run(config)
    if config.Pandas_au:
        storage.panda._autoget("followers")
        if config.User_full:
            storage.panda._autoget("user")
    if config.Pandas_clean and not config.Store_object:
        #storage.panda.clean()
        output.clean_follow_list()

def Following(config):
    logme.debug(__name__+':Following')
    output.clean_follow_list()
    config.Following = True
    config.Followers = False
    config.Profile = False
    config.Profile_full = False
    config.Favorites = False
    config.TwitterSearch = False
    run(config)
    if config.Pandas_au:
        storage.panda._autoget("following")
        if config.User_full:
            storage.panda._autoget("user")
    if config.Pandas_clean and not config.Store_object:
        #storage.panda.clean()
        output.clean_follow_list()

def Lookup(config):
    logme.debug(__name__+':Lookup')
    if config.User_id is not None:
            logme.debug(__name__+':Twint:Lookup:user_id')
            config.Username = get_event_loop().run_until_complete(get.Username(config.User_id))
    url = f"https://twitter.com/{config.Username}?lang=en"
    get_event_loop().run_until_complete(get.User(url, config, db.Conn(config.Database)))
    if config.Pandas_au:
        storage.panda._autoget("user")

def Profile(config):
    logme.debug(__name__+':Profile')
    config.Profile = True
    config.Favorites = False
    config.Following = False
    config.Followers = False
    config.TwitterSearch = False
    run(config)
    if config.Pandas_au:
        storage.panda._autoget("tweet")

def Search(config, callback=None):
    logme.debug(__name__+':Search')
    config.TwitterSearch = True
    config.Favorites = False
    config.Following = False
    config.Followers = False
    config.Profile = False
    config.Profile_full = False
    run(config, callback)
    if config.Pandas_au:
        storage.panda._autoget("tweet")
