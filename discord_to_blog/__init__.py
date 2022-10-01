#!/usr/bin/env python

import asyncio
from collections import deque
import os.path
from datetime import datetime, timedelta
import logging
import re
import shutil
import subprocess
from urllib.parse import urlparse

import discord
import pkg_resources
import pelican
import pelican.settings
import pelican.utils
import pytz


__log__ = logging.getLogger(__name__)


DELETE_EMOJI = "❌"
PUBLISH_EMOJI = "✅"
UNPUBLISH_EMOJI = "✏️"

HELP_END = "Once you no longer need this help, close it with the {} below.".format(DELETE_EMOJI)
HELP_TEXT = "I will publish content you post here to <{site_url}>" + """

Usage:
 - Just write a message and attach some media to post it.
 - If you start your message with `draft:`, you will have a chance to look at the post before publishing it.
 - If your message had a blank line in it, anything before the blank line will be the title, anything after will be added to the post contents.

When I create posts, I will post a message in this chat with a link to them. You can perform actions on the post by replying to the message with the following commands:
 - `publish` ({}): If the post is a draft, it will be published
 - `unpublish` ({}): If the post is published, it will be converted to a draft and hidden from the site
 - `delete` ({}): Deletes the post
 - `add`: Adds all media attached to the message to the post

I also respond to the following commands:
 - `help`: shows this message
 - `regenerate`: forces the website to refresh its contents

""".format(PUBLISH_EMOJI, UNPUBLISH_EMOJI, DELETE_EMOJI) + HELP_END

CLEAN_FILENAME = str.maketrans(" ", "_", "\\/[](){})")

VIDEO_EXTENSIONS = {"mkv", "mpg", "mpeg", "mpv", "mp4", "m4v", "mov", "webm", "gif"}
IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "svg", "heic", "heif", "bmp", "tiff", "webp"}
URL_RE = re.compile(".*<([^>]+)>")
DRAFT_PREFIX = "draft:"

POST_TEMPLATE = """\
Title: {title}
Author: {author}
Date: {date}

{content}

{media}"""

IMAGE_TEMPLATE = "[![{filename}]({{attach}}{thumb_filename})]({{static}}{filename})"
VIDEO_TEMPLATE =  f"<div class='video-thumb' markdown='1'>{IMAGE_TEMPLATE}</div>"
OTHER_TEMPLATE = "[{filename}]({{static}}{filename})"

MEDIA_MAX_DIMENSION = 800

IMPLICIT_ADD_TIMEOUT = 5
MESSAGE_DEBOUNCE_DELAY = 2
REGENERATE_DEBOUNCE_DELAY = 2

MESSAGE_DELETE_DELAY=5

PELICAN_SETTINGS = {
    "USE_FOLDER_AS_CATEGORY": False,
    "DEFAULT_PAGINATION": 5,
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

    "ARTICLE_TRANSLATION_ID": False,

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
    # Theme settings
    "THEME": pkg_resources.resource_filename(__name__, "theme"),
    "MENU_INTERNAL_PAGES": (('Archives', PELICAN_SETTINGS["ARCHIVES_URL"], PELICAN_SETTINGS["ARCHIVES_SAVE_AS"]),),
})

def call_later(seconds, callback):
    async def f():
        await asyncio.sleep(seconds)
        if asyncio.iscoroutinefunction(callback):
            await callback()
        else:
            callback()
    return asyncio.ensure_future(f())

class MyClient(discord.Client):

    def __init__(self, *args, guild_name, channel, data_dir, output_dir, base_url, site_name, timezone, **kwargs):
        self._guild_name = guild_name
        self._channel_name = channel

        self._settings = {
            "PATH": data_dir,
            "OUTPUT_PATH": output_dir,
            "SITEURL": base_url,
            "FEED_DOMAIN": base_url,
            "SITENAME": site_name,
            "TIMEZONE": timezone,
        }

        self._channel = None
        self._pelican = None

        self._queue = deque()
        self._process_task = None
        self._regenerate_task = None
        self._prev_posts = {}
        super().__init__(
            *args,
            intents=discord.Intents(
                guilds=True,
                guild_messages=True,
                guild_reactions=True,
                message_content=True,
            ),
            **kwargs
        )

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
            __log__.info("Guild %s not accessible to bot", self._guild_name)
            await self.close()
            return

        __log__.info("Logged in as %s", self.user)
        try:
            self._channel = {x.name: x for x in g.text_channels}[self._channel_name]
        except KeyError:
            __log__.info("Bot does not have access to a channel named '#%s'", self._channel_name)
            __log__.info("Logging out")
            await self.close()
            return

        __log__.info("Found channel '#%s'", self._channel_name)

        # Configure pelican settings
        PELICAN_SETTINGS.update(self._settings)
        os.makedirs(PELICAN_SETTINGS["PATH"], exist_ok=True)
        os.makedirs(PELICAN_SETTINGS["OUTPUT_PATH"], exist_ok=True)

        self._pelican = pelican.Pelican(pelican.settings.read_settings(override=PELICAN_SETTINGS))

        __log__.info("Set up the Pelican site generator")

    def regenerate(self, defer=True, clean=False):
        if self._regenerate_task:
            self._regenerate_task.cancel()

        if clean:
            pelican.utils.clean_output_dir(self._pelican.output_path, self._pelican.output_retention)

        if defer:
            self._regenerate_task = call_later(REGENERATE_DEBOUNCE_DELAY, self._pelican.run)
        else:
            self._pelican.run()

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
        except Exception as e:
            await message.reply(f"ERROR: failed to run command ({e.__class__.__name__}: {e})", delete_after=MESSAGE_DELETE_DELAY)
        return True

    async def process_message(self, message):
        # Process commands
        if await self.dispatch_command(message):
            await message.delete(delay=MESSAGE_DELETE_DELAY)
            return

        # Special case - treat immidiate non-command as an "add"
        parent = await self.find_implicit_parent(message)
        if parent:
            await self.cmd_reply_add(message, parent)
            await message.delete()
            return

        # Not a command - treat it as an upload
        title, path, is_draft = await self.make_post(message)
        if path:
            self.regenerate()
            if is_draft:
                msg = await self._channel.send(content=f"{message.author.mention} drafted a post titled \"{title}\": <{self.site_url}/{path}>")
            else:
                msg = await self._channel.send(content=f"{message.author.mention} published a post titled \"{title}\": <{self.site_url}/{path}>")
            await self.apply_reactions(msg, is_draft=is_draft)

            # Store message in case we need to implicitly add to it later
            self._prev_posts[message.author.id] = msg
        else:
            await message.reply("ERROR: Failed to post - no content or attached media", delete_after=MESSAGE_DELETE_DELAY)
        await message.delete()

    async def process_queue(self):
        lst = []
        while self._queue:
            lst.append(self._queue.popleft())

        # Sort messages by author, content above non-content, fall back to created time
        lst.sort(key=lambda x: (x.author.id, not x.clean_content.strip(), x.created_at))

        for x in lst:
            await self.process_message(x)

    async def on_message(self, message):
        # Ignore self
        if message.author == self.user:
            return
        # Ignore messages to other channels
        elif message.channel != self._channel:
            return

        # Add the message to the queue to process and wait for any followups
        # This allows for processing messages synchronously
        if self._process_task:
            self._process_task.cancel()
        self._queue.append(message)
        self._process_task = call_later(MESSAGE_DEBOUNCE_DELAY, self.process_queue)

    async def on_raw_reaction_add(self, reaction):
        # Using raw_reaction_add so we can get posts from before we connected

        # Must be a user reacting to the bot's messages in the proper channel
        if reaction.member == self.user:
            return
        message = await self._channel.fetch_message(reaction.message_id)
        if message.author != self.user or message.channel != self._channel:
            return

        emoji = str(reaction.emoji)

        # Special case for the help text
        if message.clean_content.endswith(HELP_END):
            if emoji == DELETE_EMOJI:
                await message.delete()
            return

        fcn = {
            DELETE_EMOJI: self.cmd_reply_delete,
            UNPUBLISH_EMOJI: self.cmd_reply_unpublish,
            PUBLISH_EMOJI: self.cmd_reply_publish,
        }.get(emoji)
        if not fcn:
            return

        message = await fcn(message, message)

        if message is None:
            # was deleted
            return

        post = self.get_post_data(message)
        if not post:
            await message.clear_reactions()
        else:
            await self.apply_reactions(message, is_draft=post["is_draft"])

    async def apply_reactions(self, message, is_draft):
        await message.clear_reactions()
        await message.add_reaction(DELETE_EMOJI)
        if is_draft:
            await message.add_reaction(PUBLISH_EMOJI)
        else:
            await message.add_reaction(UNPUBLISH_EMOJI)

    async def find_implicit_parent(self, message):
        # Only implicitly add if the message has no text in it
        if message.clean_content.strip():
            return None

        msg = self._prev_posts.get(message.author.id)
        if not msg:
            return None

        # If the message is too old then don't add to it
        if message.created_at - (msg.edited_at or msg.created_at) > timedelta(seconds=IMPLICIT_ADD_TIMEOUT):
            return None

        return msg

    def get_datetime(self, message):
        return message.created_at.astimezone(pytz.timezone(self._settings["TIMEZONE"]))

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

    async def save_attachment_thumb(self, attachment, filename, path):
        # Attempt to get a thumbnail
        #  - Only attempt for attachments with width+height (images and videos)
        #  - Abuses the fact that the proxy_url takes format, width, and height params
        #  - Only keeps it if the thumbnail is actually smaller
        w, h = attachment.width, attachment.height
        if w is None or h is None:
            return None

        # Don't allow thumbnails to go bigger than the originals
        scale = max(1, max(w, h) / MEDIA_MAX_DIMENSION)

        thumb_filename = "{}.{}px.jpg".format(os.path.splitext(filename)[0], MEDIA_MAX_DIMENSION)
        thumb_path = os.path.join(self.data_dir, path, thumb_filename)
        with open(thumb_path, 'wb') as f:
            attachment.proxy_url = "{}?format=jpeg&width={:g}&height={:g}".format(attachment.proxy_url, w // scale, h // scale)
            await attachment.save(f, use_cached=True)

        # If the thumbnail is bigger, just use the original file as the thumb
        # NOTE: If a thumbnail for a video is ever bigger than the entire video this will cause
        #       the original video to be linked in the <img> tag (and not display).
        if os.path.getsize(thumb_path) >= attachment.size:
            os.remove(thumb_path)
            return filename

        return thumb_filename

    async def save_attachments(self, message, path):
        saved = []
        for a in message.attachments:
            filename = os.path.basename(urlparse(a.url).path).translate(CLEAN_FILENAME)

            # Don't overwrite files if they already exist (find a new filename by repeatly appending '_')
            while os.path.exists(os.path.join(self.data_dir, path, filename)):
                r, e = os.path.splitext(filename)
                filename = f"{r}_{e}"

            with open(os.path.join(self.data_dir, path, filename), 'wb') as f:
                await a.save(f)

            thumb_filename = await self.save_attachment_thumb(a, filename, path)

            saved.append({
                "filename": filename,
                "thumb_filename": thumb_filename,
            })

        return saved

    @staticmethod
    def embed_media(media):
        template = OTHER_TEMPLATE
        # Only video and images have thumbnails
        if media["thumb_filename"] is not None:
            ext = media["filename"].rsplit(".", 1)[-1].lower()
            if ext in VIDEO_EXTENSIONS:
                template = VIDEO_TEMPLATE
            elif ext in IMAGE_EXTENSIONS:
                template = IMAGE_TEMPLATE
        return template.format(**media)

    async def make_post(self, message):
        date = self.get_datetime(message)
        title, content, is_draft = self.parse_text(message)
        path = self.get_path(date, is_draft)

        os.makedirs(os.path.join(self.data_dir, path), exist_ok=True)

        media = await self.save_attachments(message, path)
        if not media and not content:
            return None, None, None

        output = POST_TEMPLATE.format(
            title=title,
            date=date.strftime("%Y-%m-%d %H:%M:%S"),
            author=message.author.display_name,
            media="\n".join(self.embed_media(x) for x in media),
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

    async def cmd_reply_add(self, message, parent):
        post = self.get_post_data(parent)
        path = post["url_path"]
        media = await self.save_attachments(message, path)
        if not media:
            await message.reply("ERROR: No attached media to add", delete_after=MESSAGE_DELETE_DELAY)
            return parent

        with open(os.path.join(self.data_dir, path, "index.md"), 'at') as f:
            f.write("\n")
            f.write("\n".join(self.embed_media(x) for x in media))

        self.regenerate()
        await message.reply(f"Added media to post <{self.site_url}/{path}>", delete_after=MESSAGE_DELETE_DELAY)
        return parent

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
            self.regenerate()
            await parent.delete(delay=MESSAGE_DELETE_DELAY)
            await message.reply(f"Deleted post <{self.site_url}/{path}>", delete_after=MESSAGE_DELETE_DELAY)
            return None
        else:
            await message.reply(f"ERROR: Failed to delete post", delete_after=MESSAGE_DELETE_DELAY)
            return parent

    async def cmd_reply_publish(self, message, parent):
        post = self.get_post_data(parent)
        if not post["is_draft"]:
            await message.reply(f"ERROR: Post is not a draft", delete_after=MESSAGE_DELETE_DELAY)
            return parent

        try:
            shutil.rmtree(post["output_dir"])
        except Exception:
            pass

        path = self.get_path(post["date"], is_draft=False)
        try:
            shutil.move(post["input_dir"], os.path.join(self.data_dir, path))
        except Exception:
            await message.reply(f"ERROR: Failed to publish post", delete_after=MESSAGE_DELETE_DELAY)
            return parent

        self.regenerate()
        await message.reply(f"Published post", delete_after=MESSAGE_DELETE_DELAY)
        return await parent.edit(content=parent.content.replace("drafted a post", "published a post").replace(post["url_path"], path))

    async def cmd_reply_unpublish(self, message, parent):
        post = self.get_post_data(parent)
        if post["is_draft"]:
            await message.reply(f"ERROR: Post is already a draft", delete_after=MESSAGE_DELETE_DELAY)
            return parent

        try:
            shutil.rmtree(post["output_dir"])
        except Exception:
            pass

        path = self.get_path(post["date"], is_draft=True)
        try:
            shutil.move(post["input_dir"], os.path.join(self.data_dir, path))
        except Exception:
            await message.reply(f"ERROR: Failed to unpublish post", delete_after=MESSAGE_DELETE_DELAY)
            return parent

        self.regenerate()
        await message.reply(f"Unpublished post", delete_after=MESSAGE_DELETE_DELAY)
        return await parent.edit(content=parent.content.replace("published a post", "drafted a post").replace(post["url_path"], path))

    async def cmd_regenerate(self, message):
        self.regenerate(defer=False, clean=True)
        await message.reply("Regenerated content", delete_after=MESSAGE_DELETE_DELAY)

    async def cmd_help(self, message):
        msg = await message.reply(HELP_TEXT.format(site_url=self.site_url))
        await msg.add_reaction(DELETE_EMOJI)


def run_blogbot(**conf):
    discord.utils.setup_logging(level=logging.INFO)

    token = conf.pop("token")

    client = MyClient(**conf)
    client.run(token, log_handler=None)
