const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

// Minimal DOM for the pure report renderers. No network or live load generation.
function ui() {
  class Element {
    constructor() {this.children = [];this.textContent = '';}
    append(...children) {this.children.push(...children);}
    replaceChildren(...children) {this.children = children;}
    setAttribute() {}
    text() {return this.textContent + ' ' + this.children.map(c => c.text()).join(' ');}
  }
  const elements = new Map();
  const document = {
    getElementById(id) {if(!elements.has(id)) elements.set(id,new Element());return elements.get(id);},
    createElement() {return new Element();},
  };
  const context = vm.createContext({document, console});
  const source = fs.readFileSync(path.join(__dirname,'../frontend/app.js'),'utf8');
  // Keep initialization explicit: exercise rendering without starting polling.
  vm.runInContext(source.replace('init().catch(notify);',''),context);
  return {context, elements};
}

test('in-flight TTFT is rendered from progress without final summary', () => {
  const {context,elements}=ui();
  context.data={summary:null,progress:{requests:0,failed:0,active:1,peak_in_flight:1,
    waiting_first_token:0,streaming:1,observed_ttft_ms:{count:1,mean:123,p95:123}}};
  vm.runInContext('metrics(data)',context);
  const text=elements.get('metrics').text();
  assert.match(text,/123/);
  assert.match(text,/实时快照/);
  assert.match(text,/已观测 TTFT 1 个请求/);
});

test('final summary wins over stale progress', () => {
  const {context,elements}=ui();
  context.data={summary:{requests:2,failed:0,peak_in_flight:1,ttft_ms:{count:2,mean:456,p95:456}},
    progress:{requests:0,observed_ttft_ms:{mean:123}}};
  vm.runInContext('metrics(data)',context);
  const text=elements.get('metrics').text();
  assert.match(text,/456/);
  assert.doesNotMatch(text,/123/);
  assert.match(text,/最终结果/);
});

test('report explains absent usage and preserves unknown token counts', () => {
  const {context,elements}=ui();
  context.summary={requests:1,output_usage_coverage:0,token_totals:{},metric_unavailable:{'tpot_ms:missing_completion_usage':1}};
  vm.runInContext('renderReport(summary)',context);
  const text=elements.get('report').text();
  assert.match(text,/尚无成功请求返回 completion token/);
  assert.match(text,/missing_completion_usage/);
  assert.match(text,/未缓存输入/);
  assert.match(text,/—/);
});

test('selecting a provider applies endpoint and clears another provider key', () => {
  const {context,elements}=ui();
  const inputs={base_url:{value:''},model:{value:''}};
  elements.get('config').elements={namedItem(name){return inputs[name];}};
  context.document.getElementById('provider-select').value='saved';
  context.document.getElementById('api-key').value='old-key';
  vm.runInContext("providers=[{id:'saved',name:'Local',base_url:'http://localhost/v1',model:'m',has_api_key:true}]; $('provider-select').onchange();",context);
  assert.equal(inputs.base_url.value,'http://localhost/v1');
  assert.equal(inputs.model.value,'m');
  assert.equal(elements.get('api-key').value,'');
  assert.match(elements.get('provider-info').textContent,/已保存 API Key/);
});
