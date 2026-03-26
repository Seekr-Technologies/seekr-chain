import yaml


class LiteralSafeDumper(yaml.SafeDumper):
    pass


def str_presenter(dumper, data):
    """Use '|' literal style for any str that contains a newline."""
    if "\n" in data:  # only touch real multi-line strings
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


LiteralSafeDumper.add_representer(str, str_presenter)
