#!/usr/bin/env python3

from pathlib import Path


def main():
    path = Path(".")
    for item in sorted(path.glob("**/*")):
        if item.is_file() and str(item.absolute()) != __file__:
            print(item.relative_to(path))
            with open(item, "r") as f:
                print(f.read())


if __name__ == "__main__":
    main()
