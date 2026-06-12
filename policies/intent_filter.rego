# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

package nomos.intent

import rego.v1

default allow := false

allow if count(deny) == 0

decision := {
	"allow": allow,
	"reasons": reasons,
	"guidance": guidance,
	"metadata": {
		"policy": "nomos.intent",
		"policy_id": policy_id,
		"version": "1",
	},
}

reasons := [reason | deny[reason]]

guidance := [item | remediation[item]]

blank(value) if value == ""

blank(value) if trim(value, " \t\r\n") == ""

sensitive_paths := {
	"/etc/shadow",
	"/app/keys",
	"/root",
	".ssh",
	".aws",
	".gnupg",
}

shell_builtins := {"cd"}

shell_builtin(word) if {
	word in shell_builtins
}

policy_id := object.get(object.get(input, "council", {}), "opa_policy_id", "strict-shell")

valid_policy_ids := {
	"strict-shell",
	"git-workflow-local",
	"read-only-relaxed",
}

default_git_workflow_words := {
	"cat",
	"gh",
	"git",
	"grep",
	"head",
	"ls",
	"pwd",
	"sed",
	"tail",
	"wc",
}

default_read_only_words := {
	"cat",
	"grep",
	"head",
	"ls",
	"pwd",
	"sed",
	"tail",
	"wc",
}

configured_relaxed_word(word) if {
	some configured in object.get(object.get(input, "council", {}), "opa_relaxed_bare_command_words", [])
	word == configured
}

configured_relaxed_working_dir_allowed if {
	prefixes := object.get(object.get(input, "council", {}), "opa_relaxed_working_dir_prefixes", [])
	some prefix in prefixes
	startswith(input.intent.working_directory, prefix)
}

git_workflow_relaxed_word(word) if {
	policy_id == "git-workflow-local"
	configured_relaxed_working_dir_allowed
	word in default_git_workflow_words
}

configured_git_workflow_relaxed_word(word) if {
	policy_id == "git-workflow-local"
	configured_relaxed_working_dir_allowed
	configured_relaxed_word(word)
}

read_only_relaxed_word(word) if {
	policy_id == "read-only-relaxed"
	configured_relaxed_working_dir_allowed
	word in default_read_only_words
}

configured_read_only_relaxed_word(word) if {
	policy_id == "read-only-relaxed"
	configured_relaxed_working_dir_allowed
	configured_relaxed_word(word)
}

relaxed_non_absolute_shell_command_word(word) if {
	git_workflow_relaxed_word(word)
}

relaxed_non_absolute_shell_command_word(word) if {
	configured_git_workflow_relaxed_word(word)
}

relaxed_non_absolute_shell_command_word(word) if {
	read_only_relaxed_word(word)
}

relaxed_non_absolute_shell_command_word(word) if {
	configured_read_only_relaxed_word(word)
}

non_absolute_shell_command_words contains word if {
	input.intent.command_type == "shell"
	some word in input.intent.shell_command_words
	not startswith(word, "/")
	not shell_builtin(word)
}

disallowed_non_absolute_shell_command_words contains word if {
	some word in non_absolute_shell_command_words
	not relaxed_non_absolute_shell_command_word(word)
}

deny contains sprintf("Unknown OPA policy profile %q.", [policy_id]) if {
	not policy_id in valid_policy_ids
}

deny contains "Production intents require justification." if {
	input.intent.target_environment == "production"
	blank(input.intent.justification)
}

deny contains "Production intents require a blast radius." if {
	input.intent.target_environment == "production"
	blank(input.intent.blast_radius)
}

deny contains sprintf("Shell commands must invoke executables by absolute path; non-absolute command word(s): %s", [concat(", ", words)]) if {
	words := sort([word | some word in disallowed_non_absolute_shell_command_words])
	count(words) > 0
}

remote_fetch_piped_to_interpreter if {
	regex.match(`(?i)(^|[/\s])(curl|wget)[^|;]*\|\s*(/[^\s|;]*/)?(sh|bash|python|python3)(\s|$)`, input.intent.command)
}

deny contains "Remote-fetched scripts must not be piped into interpreters." if {
	remote_fetch_piped_to_interpreter
}

deny contains sprintf("Intent command references sensitive path %q.", [path]) if {
	some path in sensitive_paths
	some ref in object.get(input.intent, "normalized_path_refs", [])
	sensitive_path_match(ref, path)
}

sensitive_path_match(ref, path) if {
	ref == path
}

sensitive_path_match(ref, path) if {
	startswith(ref, sprintf("%s/", [path]))
}

sensitive_path_match(ref, path) if {
	not startswith(path, "/")
	contains(ref, sprintf("/%s/", [path]))
}

sensitive_path_match(ref, path) if {
	not startswith(path, "/")
	startswith(ref, sprintf("%s/", [path]))
}

sensitive_path_match(ref, path) if {
	not startswith(path, "/")
	endswith(ref, sprintf("/%s", [path]))
}

deny contains "Packaged file paths must be relative." if {
	some file in input.intent.packaged_files
	startswith(file.path, "/")
}

deny contains "Packaged file paths must not escape the working directory." if {
	some file in input.intent.packaged_files
	contains(file.path, "../")
}

deny contains "Packaged files must include SHA-256 digests." if {
	some file in input.intent.packaged_files
	blank(file.sha256)
}

remediation contains "Provide production justification and an honest blast radius." if {
	input.intent.target_environment == "production"
	blank(input.intent.justification)
}

remediation contains "Provide production justification and an honest blast radius." if {
	input.intent.target_environment == "production"
	blank(input.intent.blast_radius)
}

remediation contains "Use absolute executable paths for every shell command segment." if {
	count(disallowed_non_absolute_shell_command_words) > 0
}

remediation contains "Configure an explicit OPA policy profile for this council." if {
	not policy_id in valid_policy_ids
}

remediation contains "Package scripts as reviewed files instead of fetching and piping them at runtime." if {
	remote_fetch_piped_to_interpreter
}

remediation contains "Use a public interface for keys/secrets or narrow the target path." if {
	some path in sensitive_paths
	some ref in object.get(input.intent, "normalized_path_refs", [])
	sensitive_path_match(ref, path)
}
