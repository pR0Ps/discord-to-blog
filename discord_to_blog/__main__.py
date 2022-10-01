#!/usr/bin/env python

import argparse

from discord_to_blog import run_blogbot

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

    args = parser.parse_args()
    conf = yaml.safe_load(args.config.read())

    run_blogbot(**conf)


if __name__ == "__main__":
    main()
