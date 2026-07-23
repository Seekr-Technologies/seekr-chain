{
  description = "Two-node all-reduce bandwidth test on ROCm via nix closure";

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

          python = pkgs.python3.withPackages (ps: [ ps.torch ]);

          # Wrap torchrun with the RCCL tuning RoCE clusters need. Without
          # these vars in the environment, multi-node all-reduce caps at
          # ~37 GB/s single-NIC (RoCE v1 GID + same-NIC channel placement).
          # With them, ~280-300 GB/s on Mellanox CX-6/CX-7 fabrics.
          #
          # Defaults via `:=`, not `=`: if a caller sets NCCL_IB_GID_INDEX
          # in their pod env (e.g. for a different RoCE fabric or to
          # disable a knob), the wrapper respects it. The closure provides
          # the "known good on this cluster" defaults; the runtime can
          # override per-deployment.
          tuned-torchrun = pkgs.writeShellScriptBin "torchrun" ''
            : ''${NCCL_IB_GID_INDEX:=3}
            : ''${NCCL_CROSS_NIC:=1}
            : ''${NCCL_NCHANNELS_PER_PEER:=8}
            export NCCL_IB_GID_INDEX NCCL_CROSS_NIC NCCL_NCHANNELS_PER_PEER
            exec ${python}/bin/torchrun "$@"
          '';
        in
        pkgs.buildEnv {
          name = "seekr-chain-nix-bandwidth-test-env";
          # tuned-torchrun gets hiPrio so its bin/torchrun wins the
          # buildEnv collision with python's bin/torchrun. Without
          # hiPrio, buildEnv would error on the duplicate name.
          paths = [
            (pkgs.lib.hiPrio tuned-torchrun)
            python
            pkgs.bash
            pkgs.coreutils
            pkgs.cacert
            # rdma-core provides libibverbs. RCCL dlopens libibverbs at
            # init time to talk to the IB/RoCE stack; without it RCCL
            # falls back to TCP and caps at single-digit GB/s.
            pkgs.rdma-core
          ];
        };
    in
    {
      packages = forAllSystems (system: { default = mkEnv system; });
    };
}
