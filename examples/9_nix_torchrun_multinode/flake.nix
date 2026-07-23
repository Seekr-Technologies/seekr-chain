{
  description = "Two-node torchrun on ROCm via a nix closure";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f system);

      mkEnv = system:
        let
          pkgs = import nixpkgs {
            inherit system;
            config.allowUnfree = true;
            config.rocmSupport = true;
          };

          python = pkgs.python3.withPackages (ps: [
            ps.torch
          ]);
        in
        pkgs.buildEnv {
          name = "seekr-chain-nix-torchrun-multinode-env";
          paths = [
            python
            pkgs.rocmPackages.rocm-smi
            pkgs.bash
            pkgs.coreutils
            pkgs.cacert
            # See examples/8_nix_torchrun/flake.nix — same standard-tools
            # opt-in for grep/sed/find that the nix-runner image doesn't
            # provide by default.
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
