discord-to-blog
===============

A Discord bot that generates a static blog from posts in a channel.

The images and posts in the Discord channel will be deleted as they are posted to the blog.

Motivation
----------
I wrote this so that myself and others would be able to easily publish images to a blog. Since we
already use Discord, I wanted to experiment with using it as the input mechanism.

Since this is an experiment, it's in a "mostly works" state. There are probably a bunch of unhandled
edge cases and it bundles its own hardcoded theme ([Blue Penguin][]).

Instructions
------------
1. Create a Discord bot (see <https://discordpy.readthedocs.io/en/latest/discord.html>) with the
   following permissions:
    - Manage Messages (to delete messages)
    - Send Messages
2. Install `discord-to-blog` using `pip` (`pip install git+https://github.com/pR0Ps/discord-to-blog`)
3. Create a `settings.yml` file based off the example below:
```
guild_name: ...
token: ...
channel: blog-posts
data_dir: ./content
output_dir: ./output
base_url: http://localhost:8000
site_name: My photo blog
timezone: America/Toronto
```
4. Run `discord-to-blog --config <path to settings.yml>`.
5. Send messages to the specified channel and watch as the website is generated in the output directory.
6. Serve up the output directory using a web server of some kind.
7. [optional] Configure your system to run `discord-to-blog` as a service using something like
   `systemd` to ensure that it runs at startup, restarts if it crashes, etc.


Licence
-------
Licensed under the [GNU GPLv3][]


  [GNU GPLv3]: https://www.gnu.org/licenses/gpl-3.0.en.html
  [Blue Penguin]: https://github.com/jody-frankowski/blue-penguin/
