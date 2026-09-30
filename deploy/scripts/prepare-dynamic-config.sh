#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Share the dynamic catalog with the council's supplementary group.
set -Eeuo pipefail

directory="${1:?dynamic config directory required}"
config_gid="${2:?council config group ID required}"
[[ "$config_gid" =~ ^[0-9]+$ ]] || {
  printf 'COUNCIL_CONFIG_GID must be a numeric group ID\n' >&2
  exit 1
}
[[ -d "$directory" ]] || exit 0

# Only touch entries owned by the operator, and never follow symlinks. Files
# created by the council already belong to its UID. Setgid on directories
# makes new files inherit the shared group, including atomic replacements.
owner_uid="$(id -u)"
if ! find "$directory" -user "$owner_uid" ! -type l -exec chgrp "$config_gid" {} +; then
  printf 'Cannot assign COUNCIL_CONFIG_GID=%s; choose a group your account belongs to.\n' "$config_gid" >&2
  exit 1
fi
find "$directory" -user "$owner_uid" ! -type l -exec chmod u+rwX,g+rwX,o-rwx {} +
find "$directory" -user "$owner_uid" -type d -exec chmod g+s {} +
# A directory owned by another account may still have the old public-write
# permissions. Its owner must secure it before this bootstrap can continue.
if [[ -n "$(find "$directory" -maxdepth 0 -perm -0002 -print)" ]]; then
  printf 'Dynamic config directory is still writable by other users: %s. Ask its owner to remove public write access.\n' "$directory" >&2
  exit 1
fi
