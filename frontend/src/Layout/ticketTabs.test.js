import assert from "node:assert/strict";
import * as FlexLayout from "flexlayout-react";
import {defaultLayout} from "./defaultLayout.js";
import {tabComponent, tabExists} from "./ticketTabs.js";

const ticketId = "11111111-1111-4111-8111-111111111111";
const model = FlexLayout.Model.fromJson(structuredClone(defaultLayout));

assert.equal(tabExists(model, ticketId), false);
assert.equal(tabComponent(model, ticketId), undefined);

model.doAction(FlexLayout.Actions.addNode({
    type: "tab",
    component: "GuacViewer",
    name: "ssh box",
    id: ticketId,
}, "tabset_main", FlexLayout.DockLocation.CENTER, -1));

assert.equal(tabExists(model, ticketId), true);
assert.equal(tabComponent(model, ticketId), "GuacViewer");

const node = model.getNodeById(ticketId);
assert.equal(node.getId(), ticketId);
assert.equal(node._attributes, undefined);

// A second open of the same ticket must see the existing tab.
assert.equal(tabExists(model, ticketId), true);
const tabs = [];
model.visitNodes((visited) => {
    if (visited.getId() === ticketId) {
        tabs.push(visited);
    }
});
assert.equal(tabs.length, 1);

console.log("ticketTabs ok");
