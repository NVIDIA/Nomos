#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Share the dynamic catalog with the council's supplementary group.
set -Eeuo pipefail

directory="${1:?dynamic config directory required}"
config_gid="${2:?council config group ID required}"
# Matches the explicit council UID in deploy/Dockerfile. An optional UID allows
# callers using a different runtime identity to validate its effective access.
council_uid="${3:-1000}"
[[ "$council_uid" =~ ^[0-9]+$ ]] || {
  printf 'Council runtime UID must be numeric\n' >&2
  exit 1
}
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
# The council can use owner permissions on files it wrote itself (atomic YAML
# writes use 0600). Other entries need the supplementary group's permissions.
# Owner permissions take precedence over group permissions for the same UID.
# Keep symlinks excluded, and check directories for traversal/replacement access.
inaccessible="$(find "$directory" \( \
  \( -user "$council_uid" \( \( -type d ! -perm -0700 \) -o \
    \( -type f ! -perm -0600 \) \) \) -o \
  \( ! -user "$council_uid" \( \
    \( -type d \( ! -group "$config_gid" -o ! -perm -0070 \) \) -o \
    \( -type f \( ! -group "$config_gid" -o ! -perm -0060 \) \) \) \) \) -print)"
if [[ -n "$inaccessible" ]]; then
  printf 'Dynamic config entries lack access for council UID=%s and COUNCIL_CONFIG_GID=%s:\n%s\nAsk their owner to repair group ownership with chgrp and grant group rwX access with chmod; council-owned entries need owner rwX access.\n' "$council_uid" "$config_gid" "$inaccessible" >&2
  exit 1
fi
