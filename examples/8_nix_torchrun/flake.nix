{
  description = "Single-node torchrun on ROCm via a nix closure";

  # nixos-26.05 has rocm-smi 7.2.3 and pytorch-rocm built against the
  # same ROCm release. See example 7's flake.nix for version rationale.
  # Pin via flake.lock once `nix flake lock` runs.
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f system);

      mkEnv = system:
        let
          # rocmSupport + allowUnfree tells nixpkgs to pick the ROCm
          # variant of any package that has CUDA/ROCm switches — most
          # importantly, pytorch.
          pkgs = import nixpkgs {
            inherit system;
            config.allowUnfree = true;
            config.rocmSupport = true;
          };

          python = pkgs.python3.withPackages (ps: [
            ps.torch  # rocm variant via config.rocmSupport above
          ]);
        in
        pkgs.buildEnv {
          name = "seekr-chain-nix-torchrun-env";
          paths = [
            python
            # rocm-smi is handy for the script to print GPU state at startup.
            # Drop it from the closure if you want to shrink size further;
            # torch itself doesn't need the SMI binary at runtime.
            pkgs.rocmPackages.rocm-smi
            pkgs.bash
            pkgs.coreutils
            pkgs.cacert
            # Closures that shell out to standard POSIX tools (rocm-smi
            # uses grep, torchrun spawns subprocesses with various tool
            # expectations) need to ship them — the nix-runner image only
            # carries a minimal busybox set.
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
