{
  description = "Example nix closure for seekr-chain's nix-mode runtime";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-24.05";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f system);

      mkEnv = system:
        let
          pkgs = import nixpkgs { inherit system; };
          python = pkgs.python312.withPackages (ps: with ps; [ requests ]);
        in
        pkgs.buildEnv {
          name = "seekr-chain-example-nix-env";
          paths = [
            python
            pkgs.coreutils
            pkgs.bash
            # cacert exposes /etc/ssl/certs/ca-bundle.crt at the env root so
            # the TLS env vars seekr-chain sets in nix-mode pods
            # (SSL_CERT_FILE, REQUESTS_CA_BUNDLE, NIX_SSL_CERT_FILE) resolve
            # to a real file. nss-cacert is a transitive dep of `requests`
            # already, but transitive deps aren't re-exposed in buildEnv —
            # listing it here makes the env tree merge happen.
            pkgs.cacert
          ];
        };
    in
    {
      packages = forAllSystems (system: { default = mkEnv system; });
    };
}
