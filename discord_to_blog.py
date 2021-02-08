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
import pytz
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
IMAGE_TEMPLATE = "[![{filename}]({{attach}}{thumb_filename})]({{static}}{filename})"
IMAGE_MAX_DIMENSION = 800

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

    async def dispatch_command(self, message):
        command = message.clean_content.strip().lower()
        is_reply = message.reference is not None

        if " " in command:
            # fast fail for garbage
            fcn = None
        elif is_reply:
            fcn = getattr(self, f"cmd_reply_{command}", None)
        else:
            fcn = getattr(self, f"cmd_{command}", None)

        if not fcn:
            if is_reply:
                # Consider the message handled if it's an invalid reply
                await message.reply(f"ERROR: unknown reply action '{command}'", delete_after=MESSAGE_DELETE_DELAY)
                return True
            return False

        kwargs = {
            "message": message
        }

        if is_reply:
            kwargs["parent"] = await message.channel.fetch_message(message.reference.message_id)

        try:
            await fcn(**kwargs)
        except TypeError:
            await message.reply(f"ERROR: wrong arguments for command '{command}'", delete_after=MESSAGE_DELETE_DELAY)
        except Exception as e:
            await message.reply(f"ERROR: failed to run command ({e.__class__.__name__}: {e})", delete_after=MESSAGE_DELETE_DELAY)
        return True

    async def on_message(self, message):
        # Ignore self
        if message.author == self.user:
            return
        # Ignore messages to other channels
        elif message.channel != self._channel:
            return

        # Process commands
        if await self.dispatch_command(message):
            await message.delete(delay=MESSAGE_DELETE_DELAY)
            return

        # Not a command - treat it as an upload
        title, path = await self.make_post(message)
        if path:
            self._pelican.run()
            await self._channel.send(content=f"{message.author.mention} created a post titled \"{title}\": <{self._base_url}/{path}>")
        else:
            await message.reply(f"ERROR: failed to make post", delete_after=MESSAGE_DELETE_DELAY)
        await message.delete()

    def get_datetime(self, message):
        return pytz.timezone(self._settings["TIMEZONE"]).fromutc(message.created_at)

    @staticmethod
    def get_path(date):
        return date.strftime("%Y/%m/%d/%H-%M-%S").replace("/", os.sep)

    @staticmethod
    def parse_text(message):
        content = message.clean_content.strip()

        title, *content = content.split("\n\n", 1)
        content = content[0] if content else ""
        if len(title) > 72:
            content = f"{title}\n\n{content}"
            title = "{}\N{HORIZONTAL ELLIPSIS}".format(title[:72])
        elif not title:
            content = ""
            title = "Untitled post"

        return title, content

    async def save_images(self, message, path):
        images = []
        for a in message.attachments:
            filename = os.path.basename(urlparse(a.url).path).translate(CLEAN_FILENAME)
            with open(os.path.join(self._data_dir, path, filename), 'wb') as f:
                await a.save(f)

            # Attempt to get a thumbnail
            #  - Only attempted if the image is too big
            #  - Abuses the fact that the proxy_url takes width and height params
            #  - Only keeps it if the thumbnail is smaller (ex: gif thumbs can be bigger)
            thumb_filename = filename
            scale = max(a.width, a.height) / IMAGE_MAX_DIMENSION
            if scale > 1:
                thumb_filename = "thumb_{}".format(filename)
                thumb_path = os.path.join(self._data_dir, path, thumb_filename)
                with open(thumb_path, 'wb') as f:
                    a.proxy_url = "{}?width={:g}&height={:g}".format(a.proxy_url, a.width // scale, a.height // scale)
                    await a.save(f, use_cached=True)
                if os.path.getsize(thumb_path) >= a.size:
                    os.remove(thumb_path)
                    thumb_filename = filename

            images.append({"filename": filename, "thumb_filename": thumb_filename})
        return images

    async def make_post(self, message):
        date = self.get_datetime(message)
        title, content = self.parse_text(message)
        path = self.get_path(date)

        os.makedirs(os.path.join(self._data_dir, path), exist_ok=True)

        images = await self.save_images(message, path)
        if not images:
            await message.reply("ERROR: No valid attachments", delete_after=MESSAGE_DELETE_DELAY)
            return None, None, None

        output = POST_TEMPLATE.format(
            title=title,
            date=date.strftime("%Y-%m-%d %H:%M:%S"),
            author=message.author.display_name,
            images=" ".join(IMAGE_TEMPLATE.format(**x) for x in images),
            content=content,
        )
        with open(os.path.join(self._data_dir, path, "index.md"), 'wt') as f:
            f.write(output)

        return title, path

    def get_post_data(self, message):
        m = URL_RE.match(message.clean_content)
        if not m:
            return None
        url = m.group(1)
        if not url:
            return None

        path = urlparse(url).path[1:]

        out_dir = os.path.join(self._output_dir, path)
        in_dir = os.path.join(self._data_dir, path)

        return {
            "output_dir": out_dir,
            "input_dir": in_dir,
            "url_path": path,
        }

    async def cmd_reply_delete(self, message, parent):
        post = self.get_post_data(parent)
        path = post["url_path"]
        for k in ("input_dir", "output_dir"):
            if os.path.exists(post[k]):
                try:
                    shutil.rmtree(post[k])
                except Exception:
                    path = None

        if path:
            self._pelican.run()
            await parent.delete(delay=MESSAGE_DELETE_DELAY)
            await message.reply(f"Deleted post <{self._base_url}/{path}>", delete_after=MESSAGE_DELETE_DELAY)
        else:
            await message.reply(f"ERROR: Failed to delete post", delete_after=MESSAGE_DELETE_DELAY)


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
