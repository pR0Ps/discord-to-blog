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

HELP_TEXT = """\
I will publish content you post here to <{site_url}>.

Usage:
 - Just write a message and attach some media to post it.
 - If you start your message with `draft:`, you will have a chance to look at the post before publishing it.
 - If your message had a blank line in it, anything before the blank line will be the title, anything after will be added to the post contents.

When I create posts, I will post a message in this chat with a link to them. You can perform actions on the post by replying to the message with the following commands:
 - `publish`: If the post is a draft, it will be published
 - `unpublish`: If the post is published, it will be converted to a draft and hidden from the site
 - `delete`: Deletes the post

I also respond to the following commands:
 - `help`: shows this message


This message will self-destruct in 2 minutes.
"""

CLEAN_FILENAME = str.maketrans(" ", "_", "\\/[](){})")

URL_RE = re.compile(".*<([^>]+)>")
DRAFT_PREFIX = "draft:"

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
    "DRAFT_URL": "drafts/{date:%Y}-{date:%m}-{date:%d}-{date:%H}-{date:%M}-{date:%S}",
    "ARCHIVES_URL": "archives",

    "EXTRA_PATH_METADATA": {"drafts": {"status": "draft"}},

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
    "DRAFT_SAVE_AS": "{}/index.html".format(PELICAN_SETTINGS["DRAFT_URL"]),
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

    @property
    def output_dir(self):
        return self._settings["OUTPUT_PATH"]

    @property
    def data_dir(self):
        return self._settings["PATH"]

    @property
    def site_url(self):
        return self._settings["SITEURL"]

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
        title, path, is_draft = await self.make_post(message)
        if path:
            self._pelican.run()
            if is_draft:
                await self._channel.send(content=f"{message.author.mention} drafted a post titled \"{title}\": <{self.site_url}/{path}>")
            else:
                await self._channel.send(content=f"{message.author.mention} published a post titled \"{title}\": <{self.site_url}/{path}>")
        else:
            await message.reply(f"ERROR: failed to make post", delete_after=MESSAGE_DELETE_DELAY)
        await message.delete()

    def get_datetime(self, message):
        return pytz.timezone(self._settings["TIMEZONE"]).fromutc(message.created_at)

    @staticmethod
    def get_path(date, is_draft):
        if is_draft:
            return os.path.join("drafts", date.strftime("%Y-%m-%d-%H-%M-%S"))
        else:
            return date.strftime("%Y/%m/%d/%H-%M-%S").replace("/", os.sep)

    @staticmethod
    def from_path(path):
        is_draft = path.startswith("drafts/")
        if is_draft:
            date = datetime.strptime(path, "drafts/%Y-%m-%d-%H-%M-%S")
        else:
            date = datetime.strptime(path, "%Y/%m/%d/%H-%M-%S")
        return date, is_draft

    @staticmethod
    def parse_text(message):
        is_draft = False
        content = message.clean_content.strip()

        title, *content = content.split("\n\n", 1)
        content = content[0] if content else ""

        if title.lower().startswith(DRAFT_PREFIX):
            is_draft = True
            title = title[len(DRAFT_PREFIX):].strip()

        if len(title) > 72:
            content = f"{title}\n\n{content}"
            title = "{}\N{HORIZONTAL ELLIPSIS}".format(title[:72])
        elif not title:
            content = ""
            title = "Untitled post"

        return title, content, is_draft

    async def save_images(self, message, path):
        images = []
        for a in message.attachments:
            filename = os.path.basename(urlparse(a.url).path).translate(CLEAN_FILENAME)
            with open(os.path.join(self.data_dir, path, filename), 'wb') as f:
                await a.save(f)

            # Attempt to get a thumbnail
            #  - Only attempted if the image is too big
            #  - Abuses the fact that the proxy_url takes width and height params
            #  - Only keeps it if the thumbnail is smaller (ex: gif thumbs can be bigger)
            thumb_filename = filename
            scale = max(a.width, a.height) / IMAGE_MAX_DIMENSION
            if scale > 1:
                thumb_filename = "thumb_{}".format(filename)
                thumb_path = os.path.join(self.data_dir, path, thumb_filename)
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
        title, content, is_draft = self.parse_text(message)
        path = self.get_path(date, is_draft)

        os.makedirs(os.path.join(self.data_dir, path), exist_ok=True)

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
        with open(os.path.join(self.data_dir, path, "index.md"), 'wt') as f:
            f.write(output)

        return title, path, is_draft

    def get_post_data(self, message):
        m = URL_RE.match(message.clean_content)
        if not m:
            return None
        url = m.group(1)
        if not url:
            return None

        path = urlparse(url).path[1:]
        date, is_draft = self.from_path(path)

        out_dir = os.path.join(self.output_dir, path)
        in_dir = os.path.join(self.data_dir, path)

        return {
            "output_dir": out_dir,
            "input_dir": in_dir,
            "url_path": path,
            "is_draft": is_draft,
            "date": date,
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
            await message.reply(f"Deleted post <{self.site_url}/{path}>", delete_after=MESSAGE_DELETE_DELAY)
        else:
            await message.reply(f"ERROR: Failed to delete post", delete_after=MESSAGE_DELETE_DELAY)

    async def cmd_reply_publish(self, message, parent):
        post = self.get_post_data(parent)
        if not post["is_draft"]:
            await message.reply(f"ERROR: Post is not a draft", delete_after=MESSAGE_DELETE_DELAY)
            return

        try:
            shutil.rmtree(post["output_dir"])
        except Exception:
            pass

        path = self.get_path(post["date"], is_draft=False)
        try:
            shutil.move(post["input_dir"], os.path.join(self.data_dir, path))
        except Exception:
            await message.reply(f"ERROR: Failed to publish post", delete_after=MESSAGE_DELETE_DELAY)
            return

        self._pelican.run()
        await parent.edit(content=parent.content.replace("drafted a post", "published a post").replace(post["url_path"], path))
        await message.reply(f"Published post", delete_after=MESSAGE_DELETE_DELAY)

    async def cmd_reply_unpublish(self, message, parent):
        post = self.get_post_data(parent)
        if post["is_draft"]:
            await message.reply(f"ERROR: Post is already a draft", delete_after=MESSAGE_DELETE_DELAY)
            return

        try:
            shutil.rmtree(post["output_dir"])
        except Exception:
            pass

        path = self.get_path(post["date"], is_draft=True)
        try:
            shutil.move(post["input_dir"], os.path.join(self.data_dir, path))
        except Exception:
            await message.reply(f"ERROR: Failed to unpublish post", delete_after=MESSAGE_DELETE_DELAY)
            return

        self._pelican.run()
        await parent.edit(content=parent.content.replace("published a post", "drafted a post").replace(post["url_path"], path))
        await message.reply(f"Unpublished post", delete_after=MESSAGE_DELETE_DELAY)

    async def cmd_help(self, message):
        await message.reply(HELP_TEXT.format(site_url=self.site_url), delete_after=120)


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
