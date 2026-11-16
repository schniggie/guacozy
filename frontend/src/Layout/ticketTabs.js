// flexlayout-react 0.11 keeps tab state on the model. Open tabs are found
// with getNodeById, and a tab's component name is getComponent().

export function tabExists(model, tabid) {
    return Boolean(model.getNodeById(tabid));
}

export function tabComponent(model, tabid) {
    const node = model.getNodeById(tabid);
    if (!node) {
        return undefined;
    }
    return node.getComponent();
}
