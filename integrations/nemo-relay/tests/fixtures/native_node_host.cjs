// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

const assert = require('node:assert/strict');
const api = require(process.env.NOMOS_TEST_NODE_RELAY_MODULE);
const relayVersion = require(process.env.NOMOS_TEST_NODE_RELAY_MODULE + '/package.json').version;
assert.equal(relayVersion, '0.11.1');
const fs = require('node:fs');
const fixture = JSON.parse(fs.readFileSync(process.env.PROBE_FIXTURE, 'utf8'));
async function main() {
  const activation = await api.initialize({}, process.env.PROBE_REGISTRY);
  const stack = api.createScopeStack();
  let result;
  try { result = await api.withScopeStack(stack, async () => {
    const owner = api.pushScope('synthetic-agent', api.ScopeType.Agent);
    const codec = new api.OpenAIChatCodec();
    const decode = value => codec.decode(value);
    const encode = value => codec.encode(value.annotated, value.original);
    const decodeResponse = value => codec.decodeResponse(value);
    const request = label => ({headers:{}, content:{model:'fixture',messages:[{role:'user',content:label}],tools:[fixture.tool]}});
    const prepared = [];
    let executed = 0;
    async function local(args) {
      assert.deepEqual(args, {path:'a'});
      executed++;
      return {result:{content:'synthetic file'}};
    }
    try {
      const raw = {id:'fixture',model:'fixture',choices:[{index:0,message:{role:'assistant',content:null,tool_calls:[{id:'call_node',type:'function',function:{name:'read_file',arguments:JSON.stringify({path:'a',nomos:fixture.intent})}}]},finish_reason:'tool_calls'}]};
      await api.llmCallExecuteAsync('fixture', request('node unary'), async value => {
        prepared.push(value);
        return raw;
      },owner,null,null,null,'fixture',decode,encode,decodeResponse);
      const unary = await api.toolCallExecuteAsync('read_file',{path:'a',nomos:fixture.intent},local,owner,null,null,null,'call_node');
      assert.equal(unary.result.status,'executed',JSON.stringify(unary));
      let providerEntered = false;
      const openStream = () => api.llmStreamCallExecute('fixture',request('node stream'),async received => {
        providerEntered = true;
        prepared.push(received);
        return (async function* () {
          const argumentText = JSON.stringify({path:'a',nomos:fixture.intent});
          yield {id:'stream',model:'fixture',choices:[{index:0,delta:{tool_calls:[{index:0,id:'call_node_stream',type:'function',function:{name:'read_',arguments:argumentText.slice(0,8)}}]},finish_reason:null}]};
          yield {id:'stream',model:'fixture',choices:[{index:0,delta:{tool_calls:[{index:0,function:{name:'file',arguments:argumentText.slice(8)}}]},finish_reason:'tool_calls'}]};
          yield {id:'stream',model:'fixture',choices:[],usage:{completion_tokens:10}};
        })();
      },()=>{},()=>({choices:[]}),owner,null,null,null,'fixture',decode,encode,decodeResponse);
      if (process.env.PROBE_EXPECT_STREAM_REJECTION === '1') {
        let rejected = false;
        try {
          const unsupported = await openStream();
          try { while (await unsupported.next() !== null) {} }
          finally { await unsupported.close(); }
        } catch (error) {
          rejected = true;
          assert.match(String(error), /explicit stream_format/);
        }
        assert.equal(rejected, true);
        assert.equal(providerEntered, false);
        return {unary:'passed',stream:'rejected before provider',localExecutions:executed};
      }
      const stream = await openStream();
      try { while (await stream.next() !== null) {} }
      finally { await stream.close(); }
      const streamed = await api.toolCallExecuteAsync('read_file',{path:'a',nomos:fixture.intent},local,owner,null,null,null,'call_node_stream');
      assert.equal(streamed.result.status,'executed',JSON.stringify(streamed));
      for (const value of prepared) {
        const schema=value.content.tools[0].function.parameters.properties.nomos;
        assert.equal('turn' in schema.properties,false);
        assert.equal(JSON.stringify(value).includes('nt_'),false);
      }
      return {node:process.version,relay:relayVersion,unary:'passed',stream:'passed',localExecutions:executed,modelTrackingFieldAbsent:true};
    } finally { api.popScope(owner); }
  }); } finally { await activation.close(); }
  console.log(JSON.stringify(result));
}
main().catch(error => { console.error(error); process.exitCode=1; });
