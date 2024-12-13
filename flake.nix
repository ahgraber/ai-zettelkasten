{
  description = "A simple flake to install dependencies for ai-zk";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    devshell.url = "github:numtide/devshell";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, devshell, flake-utils, nixpkgs }:
    flake-utils.lib.eachDefaultSystem (system: {
      devShells.default =
        let
          pkgs = import nixpkgs {
            inherit system;
            # bring devshell attribute into the pkgs
            # overlays = [ devshell.overlays.default ];
          };
          nodeEnv = import ./nix/default.nix;
        in
        # pkgs.devshell.mkShell {
        pkgs.mkShell {
          name = "aizk-devshell";

          # buildInputs = [
          #   nodeEnv.nodejs
          #   nodeEnv.nodePackages."${nodeEnv.packageName}"
          # ];
          # buildPhase = ''
          #   ln -s ${nodeEnv.nodeDependencies}/lib/node_modules ./node_modules
          #   export PATH="${nodeEnv.nodeDependencies}/bin:$PATH"
          # '';

          # a list of packages to add to the shell environment
          packages = [
            #--- node ---
            pkgs.deno
            pkgs.node2nix
            # nodejs_20 # nodejs runtime v20 for v8 javascript
            #--- containers ---
            # pkgs.podman
            pkgs.colima
            pkgs.qemu
          ];
          # imports = [ (pkgs.devshell.importTOML ./devshell.toml) ];
          # shellHook = ''
          #   export PATH="${nodeEnv.packagePath}/bin:$PATH"
          #   echo "Node.js environment loaded"
          # '';
        };
    });
}
