// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
// SPDX-License-Identifier: Apache-2.0

// Real Node host + installed Python worker. No model service or external tool.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const api = require(process.env.NOMOS_TEST_NODE_RELAY_MODULE);
const fixture = JSON.parse(fs.readFileSync(process.env.PROBE_FIXTURE, 'utf8'));
const accepted = process.env.PROBE_ALLOW_SINGLE_ROOT === '1';

async function agent(label) {
  const original = api.createScopeStack();
  return api.withScopeStack(original, async () => {
    const owner = api.pushScope('synthetic-agent-' + label, api.ScopeType.Agent);
    const ownerId = api.currentScopeStackSnapshot().scopes.at(-1).scopeId;
    let providers = 0;
    let executions = 0;
    // Recreate a separate operation stack each time, as a host would do for
    // independently scheduled model and tool calls. Never reuse a mutable stack.
    const operation = callback => {
      const stack = api.createScopeStackFromPropagation(api.capturePropagationContext());
      return api.withScopeStack(stack, () => {
        const scopes = api.currentScopeStackSnapshot().scopes;
        assert.deepEqual(scopes, [{scopeId: ownerId, scopeType: api.ScopeType.Agent}]);
        return callback(api.getHandle());
      });
    };
    const codec = new api.OpenAIChatCodec();
    const decode = value => codec.decode(value);
    const encode = value => codec.encode(value.annotated, value.original);
    const decodeResponse = value => codec.decodeResponse(value);
    const request = {headers: {}, content: {model: 'fixture', messages: [{role: 'user', content: label}], tools: [fixture.tool]}};
    const args = {path: 'a', nomos: fixture.intent};
    const local = async business => {
      assert.deepEqual(business, {path: 'a'});
      executions++;
      return {result: {content: 'synthetic file'}};
    };
    try {
      for (const streaming of [false, true]) {
        // Both Agents intentionally use identical IDs; the Agent UUID must
        // keep their originating request snapshots and claims separate.
        const callId = streaming ? 'shared_stream' : 'shared_unary';
        const model = () => operation(async parent => {
          if (!streaming) {
            return api.llmCallExecuteAsync('fixture', request, async () => {
              providers++;
              return {id: 'fixture', model: 'fixture', choices: [{index: 0, message: {role: 'assistant', content: null,
                tool_calls: [{id: callId, type: 'function', function: {name: 'read_file', arguments: JSON.stringify(args)}}]}, finish_reason: 'tool_calls'}]};
            }, parent, null, null, null, 'fixture', decode, encode, decodeResponse);
          }
          const stream = await api.llmStreamCallExecute('fixture', request, async () => {
            providers++;
            return (async function* () {
              yield {id: 'fixture', model: 'fixture', choices: [{index: 0, delta: {tool_calls: [{index: 0, id: callId,
                type: 'function', function: {name: 'read_file', arguments: JSON.stringify(args)}}]}, finish_reason: 'tool_calls'}]};
            })();
          }, () => {}, () => ({choices: []}), parent, null, null, null, 'fixture', decode, encode, decodeResponse);
          try { while (await stream.next() !== null) {} }
          finally { await stream.close(); }
        });
        if (accepted) await model();
        else await assert.rejects(model, /application Agent scope/);
        const result = await operation(parent => api.toolCallExecuteAsync('read_file', args, local,
          parent, null, null, null, callId));
        assert.equal(result.result.status, accepted ? 'executed' : 'native_context_unavailable');
      }
      const ordinary = await operation(parent => api.toolCallExecuteAsync('ordinary_read', {},
        async () => ({result: {ordinary: true}}), parent));
      assert.deepEqual(ordinary.result, {ordinary: true});
      assert.equal(providers, accepted ? 2 : 0);
      assert.equal(executions, accepted ? 2 : 0);
      return {ownerId, providers, executions};
    } finally { api.popScope(owner); }
  });
}

async function main() {
  const activation = await api.initialize({}, process.env.PROBE_REGISTRY);
  try {
    const results = await Promise.all([agent('request A'), agent('request B')]);
    assert.notEqual(results[0].ownerId, results[1].ownerId);
    console.log(JSON.stringify({accepted, agents: results}));
  } finally { await activation.close(); }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
