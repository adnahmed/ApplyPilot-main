package main

import (
	_ "github.com/Adnan/ApplyPilot/tools/rclone-githubreleases/backend"
	"github.com/rclone/rclone/cmd"
	_ "github.com/rclone/rclone/cmd/config"
	_ "github.com/rclone/rclone/cmd/ls"
	_ "github.com/rclone/rclone/cmd/lsd"
	_ "github.com/rclone/rclone/cmd/lsjson"
	_ "github.com/rclone/rclone/cmd/serve/restic"
	_ "github.com/rclone/rclone/cmd/version"
	"github.com/rclone/rclone/fs"
)

func main() {
	fs.Version = "v1.75.1-applypilot"
	cmd.Main()
}
