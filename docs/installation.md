# Installation

It is recommended to install as a dependency in your project environment. You can install directly from git, or as a submodule.

## Pre-reqs:

- Kubectl

    Make sure you have `kubectl` installed and configured for your cluster

## Install from PyPI

- `uv`

   ```
   uv add seekr-chain
   ```

- `pip`

   ```
   pip install seekr-chain
   ```

## Developer Install

### Install from Git

You can use your favorite package manager to install `seekr-chain` into your development environment, directly from git:

- `uv`

   ```
   uv add git+{{ git_install_url }}@{{ recommended_version }}
   ```

- `pip`

   ```
   pip install git+{{ git_install_url }}@{{ recommended_version }}
   ```

!!! tip
    It is recommended to use a `@<version_tag>` to get a stable installation. See the releases page on GitHub for the latest stable version.
    You can also use `@main` to get the latest version, or `@dev` for the latest features.


### Install as submodule

If you think you will need to modify `seekr-chain` in conjunction with your project, it may be convenient to install as an `editable submodule`.

- In your repo, create a `submods` directory: `mkdir submods && cd submods`
- Clone repository: `git clone {{ git_install_url }}`
- Add as submodule: `git submodule add ./seekr-chain`
- Add as editable dependency:
  - `uv`:

     `uv pip install -e ./seekr-chain`

  - `pip`

     `pip install -e ./seekr-chain`
