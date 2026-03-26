def define_env(env):
    """
    Define variables and macros for MkDocs
    """
    import importlib.metadata

    try:
        version = importlib.metadata.version("seekr_chain")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"

    env.variables["version"] = version
    # Derive from mkdocs.yml config — change URLs in one place
    repo_url = env.conf["repo_url"].rstrip("/") + "/"
    site_url = env.conf["site_url"].rstrip("/")

    env.variables["project_name"] = "seekr-chain"
    env.variables["package"] = f"`{env.variables['project_name']}`"
    env.variables["repo_url"] = repo_url
    env.variables["repo_tree"] = repo_url + "tree/main/"
    env.variables["repo_blob"] = repo_url + "blob/main/"
    env.variables["examples_url"] = repo_url + "tree/main/examples/"
    env.variables["git_install_url"] = repo_url.rstrip("/") + ".git"
    env.variables["docs_url"] = site_url
    env.variables["recommended_version"] = "v0.5.0"
