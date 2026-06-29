{
  description = "ROCm smoke test — verify the pod can see AMD GPUs from a nix closure";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f system);

      mkEnv = system:
        let
          # rocmPackages live behind `allowUnfree` in nixpkgs because some
          # bits ship under non-free licenses. Closures built with this
          # config don't carry the license restriction at runtime — it's
          # only an eval-time gate.
          pkgs = import nixpkgs {
            inherit system;
            config.allowUnfree = true;
          };
        in
        pkgs.buildEnv {
          name = "seekr-chain-rocm-smi-env";
          paths = [
            # rocm-smi: the AMD equivalent of nvidia-smi. Talks to the
            # kernel driver via /dev/kfd + /dev/dri/* which seekr-chain
            # mounts into the pod when gpu_type=amd.com/gpu is requested.
            pkgs.rocmPackages.rocm-smi
            pkgs.bash
            pkgs.coreutils
            pkgs.cacert
            # rocm-smi is a Python wrapper that shells out to `grep` (and
            # likely other POSIX tools) for driver detection (`grep amdgpu
            # /proc/modules`). The nix-runner image only carries a minimal
            # busybox tool set in /bin, so closures expecting GNU tools
            # need to include them explicitly. This is closure-local —
            # other examples may need to opt in similarly.
            pkgs.gnugrep
            pkgs.gnused
            pkgs.findutils
          ];
        };
    in
    {
      packages = forAllSystems (system: { default = mkEnv system; });
    };
}
