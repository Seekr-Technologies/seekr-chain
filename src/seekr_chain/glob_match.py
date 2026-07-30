from fnmatch import fnmatch
from itertools import product


def fnmatch_glob(name: str, pattern: str) -> bool:
    """fnmatch, but '**' path components additionally match zero directory
    segments. Plain fnmatch already treats a single '*' as crossing '/'
    (matching one or more segments); '**' only needs expansion to also
    cover the zero-segments case, e.g. 'src/**/*' matching 'src/file.py'."""
    if "**" not in pattern:
        return fnmatch(name, pattern)
    return any(fnmatch(name, variant) for variant in _expand_globstar(pattern))


def _expand_globstar(pattern: str) -> list[str]:
    """All patterns obtained by independently replacing each '**' path
    component with '*' (one-or-more, via fnmatch's slash-crossing '*') or
    deleting it outright (zero). Deleting the component itself (not the
    substring) avoids stray slashes, e.g. ["src","**","*"] -> ["src","*"]
    -> "src/*", rather than leaving "src//*"."""
    parts = pattern.split("/")
    star_idxs = [i for i, p in enumerate(parts) if p == "**"]

    variants = []
    for keep_flags in product([True, False], repeat=len(star_idxs)):
        keep_by_idx = dict(zip(star_idxs, keep_flags))
        new_parts = []
        for i, p in enumerate(parts):
            if i not in keep_by_idx:
                new_parts.append(p)
            elif keep_by_idx[i]:
                new_parts.append("*")
            # else: drop this '**' component entirely
        variants.append("/".join(new_parts))
    return variants
