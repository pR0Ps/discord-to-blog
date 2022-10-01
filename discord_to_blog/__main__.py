#!/usr/bin/env python

import argparse
import logging

from discord_to_blog import MyClient

from discord.utils import setup_logging
import yaml


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
    parser.add_argument(
        "-v", "--verbose",
        dest="verbose_count",
        action="count",
        default=0,
        help="Increase log verbosity for each occurence up to 2 (default: WARNING)"
    )

    args = parser.parse_args()

    setup_logging(
        level=(logging.WARNING, logging.INFO, logging.DEBUG)[min(2, max(0, args.verbose_count))]
    )

    conf = yaml.safe_load(args.config.read())

    token = conf.pop("token")

    client = MyClient(**conf)
    client.run(token, log_handler=None)


if __name__ == "__main__":
    main()
