import argparse
import os.path
from datetime import datetime
import re
import shutil
import subprocess
from urllib.parse import urlparse

import discord
from discord.errors import Forbidden
import pelican
import pelican.settings
import yaml

CLEAN_FILENAME = str.maketrans(" ", "_", "\\/[](){})")

URL_RE = re.compile(".*<([^>]+)>")

POST_TEMPLATE = """\
Title: {title}
Author: {author}
Date: {date}

{content}

{images}
"""
IMAGE_TEMPLATE = "[![{filename}]({{attach}}{filename})]({{static}}{filename})"

PELICAN_SETTINGS = {
    "USE_FOLDER_AS_CATEGORY": False,
    "DEFAULT_PAGINATION": 10,
    "SUMMARY_MAX_LENGTH": None,
    "STATIC_PATHS": ["."],

    # Only generate pages for articles and archives
    "DIRECT_TEMPLATES": ["index", "archives"],
    "AUTHOR_SAVE_AS": '',
    "CATEGORY_SAVE_AS": '',
    "TAG_SAVE_AS": '',
    "ARTICLE_URL": "{date:%Y}/{date:%m}/{date:%d}/{date:%H}-{date:%M}-{date:%S}",
    "ARCHIVES_URL": "archives",

    # Feeds
    "FEED_ALL_ATOM": "feed.atom",
    "FEED_MAX_ITEMS": 50,
    "TRANSLATION_FEED_ATOM": None,
    "CATEGORY_FEED_ATOM": None,
    "AUTHOR_FEED_ATOM": None,
    "AUTHOR_FEED_RSS": None,
}
PELICAN_SETTINGS.update({
    "ARTICLE_SAVE_AS": "{}/index.html".format(PELICAN_SETTINGS["ARTICLE_URL"]),
    "ARCHIVES_SAVE_AS": "{}/index.html".format(PELICAN_SETTINGS["ARCHIVES_URL"]),
})
PELICAN_SETTINGS.update({
    # Blue Penguin theme settings
    "DISPLAY_FOOTER": False,
    "MENU_INTERNAL_PAGES": (('Archives', PELICAN_SETTINGS["ARCHIVES_URL"], PELICAN_SETTINGS["ARCHIVES_SAVE_AS"]),),
    "MENUITEMS": (("Feed", "/{}".format(PELICAN_SETTINGS["FEED_ALL_ATOM"])),),
})

MESSAGE_DELETE_DELAY=5

class MyClient(discord.Client):

    def __init__(self, *args, guild_name, channel, theme_dir, data_dir, output_dir, base_url, site_name, timezone, **kwargs):
        self._guild_name = guild_name
        self._channel_name = channel

        self._data_dir=data_dir
        self._output_dir=output_dir
        self._base_url=base_url

        self._settings = {
            "PATH": data_dir,
            "OUTPUT_PATH": output_dir,
            "THEME": theme_dir,
            "SITEURL": base_url,
            "FEED_DOMAIN": base_url,
            "SITENAME": site_name,
            "TIMEZONE": timezone,
        }

        self._channel = None
        self._pelican = None
        super().__init__(*args, **kwargs)

    async def on_ready(self):
        g = discord.utils.get(self.guilds, name=self._guild_name)
        if g is None:
            print("Guild {} not accessible to bot".format(self._guild_name))
            await self.logout()
            return

        print(f"Logged in as {self.user}")
        try:
            self._channel = {x.name: x for x in g.text_channels}[self._channel_name]
        except KeyError:
            print(f"Bot does not have access to a channel named '{self._channel_name}'")
            print("Logging out")
            await self.logout()
            return

        print(f"Found channel '{self._channel_name}'")

        # Configure pelican settings
        PELICAN_SETTINGS.update(self._settings)
        os.makedirs(PELICAN_SETTINGS["PATH"], exist_ok=True)
        os.makedirs(PELICAN_SETTINGS["OUTPUT_PATH"], exist_ok=True)

        self._pelican = pelican.Pelican(pelican.settings.read_settings(override=PELICAN_SETTINGS))

        print("Set up the Pelican site generator")

    async def on_message(self, message):
        # Ignore self
        if message.author == self.user:
            return
        # Ignore messages to other channels
        elif message.channel != self._channel:
            return

        if message.reference is not None:
            # Reply to an existing message
            msg = await message.channel.fetch_message(message.reference.message_id)
            content = message.content
            if content.lower() == "delete":
                path = self.delete_post(msg)
                if path:
                    self._pelican.run()
                    await msg.delete(delay=MESSAGE_DELETE_DELAY)
                    await message.reply(f"Deleted post <{self._base_url}/{path}>", delete_after=MESSAGE_DELETE_DELAY)
                    await message.delete(delay=MESSAGE_DELETE_DELAY)
                else:
                    await message.reply(f"ERROR: Failed to delete post", delete_after=MESSAGE_DELETE_DELAY)
                    await message.delete(delay=MESSAGE_DELETE_DELAY)
            else:
                await message.reply("ERROR: unknown action '{}'".format(content), delete_after=MESSAGE_DELETE_DELAY)
                await message.delete(delay=MESSAGE_DELETE_DELAY)
            return


        if not message.attachments:
            await message.reply("ERROR: No pictures attached", delete_after=MESSAGE_DELETE_DELAY)
            await message.delete(delay=MESSAGE_DELETE_DELAY)
            return

        title, path = await self.make_post(message)

        self._pelican.run()

        await self._channel.send(content=f"{message.author.mention} created a post titled \"{title}\": <{self._base_url}/{path}>")
        await message.delete()

    async def make_post(self, message):
        date = datetime.now()
        content = message.clean_content.strip()

        title, *content = content.split("\n\n", 1)
        content = content[0] if content else ""
        if not title or len(title) > 72:
            content = title
            title = "Pictures from {}".format(date.strftime("%Y-%m-%d"))

        path = date.strftime("%Y/%m/%d/%H-%M-%S").replace("/", os.sep)
        os.makedirs(os.path.join(self._data_dir, path), exist_ok=True)

        images = []
        for a in message.attachments:
            filename = os.path.basename(urlparse(a.url).path).translate(CLEAN_FILENAME)
            images.append(filename)
            with open(os.path.join(self._data_dir, path, filename), 'wb') as f:
                await a.save(f)

        output = POST_TEMPLATE.format(
            title=title,
            date=date.strftime("%Y-%m-%d %H:%M:%S"),
            author=message.author.display_name,
            images=" ".join(IMAGE_TEMPLATE.format(filename=x) for x in images),
            content=content,
        )
        with open(os.path.join(self._data_dir, path, "index.md"), 'wt') as f:
            f.write(output)

        return title, path


    def delete_post(self, message):
        m = URL_RE.match(message.clean_content)
        if not m:
            return None
        url = m.group(1)
        if not url:
            return None

        path = urlparse(url).path[1:]

        post = os.path.join(self._data_dir, path)
        article = os.path.join(self._output_dir, path)

        ret = path
        if os.path.exists(post):
            try:
                shutil.rmtree(post)
            except Exception:
                ret = None

        if os.path.exists(article):
            try:
                shutil.rmtree(article)
            except Exception:
                ret = None

        return ret


def main():
    parser = argparse.ArgumentParser(
        description="Generate a blog from a Discord channel"
    )
    parser.add_argument(
        "-c", "--config",
        help="The config file to use",
        type=argparse.FileType(mode='rt'),
        required=True
    )

    args = parser.parse_args()

    conf = yaml.safe_load(args.config.read())

    token = conf.pop("token")
    client = MyClient(**conf)
    client.run(token)

if __name__ == "__main__":
    main()
