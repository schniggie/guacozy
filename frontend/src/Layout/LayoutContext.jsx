import React, {useEffect, useRef, useState} from 'react';
import * as FlexLayout from "flexlayout-react";
import {defaultLayout} from "./defaultLayout";
import {tabComponent, tabExists as modelHasTab} from "./ticketTabs";

const LayoutContext = React.createContext([{}, () => {
}]);

const LayoutProvider = (props) => {
    const layoutRef = useRef();

    const activateTab = (tabid) => {
        state.model.doAction(FlexLayout.Actions.selectTab(tabid));
    };

    const refreshTab = (tabid) => {
        const oldComponent = tabComponent(state.model, tabid);
        if (oldComponent === undefined) {
            return;
        }
        state.model.doAction(FlexLayout.Actions.updateNodeAttributes(tabid, {component: ""}));
        setTimeout(() =>
            state.model.doAction(FlexLayout.Actions.updateNodeAttributes(tabid, {component: oldComponent})), 5);
    };

    const deleteTab = (tabid) => {
        state.model.doAction(FlexLayout.Actions.deleteTab(tabid));
    };

    const tabExists = (ticketid) => {
        return modelHasTab(state.model, ticketid);
    };

    const activateTicket = (ticketid, tabName, controlSize, controlInput) => {
        if (tabExists(ticketid)) {
            activateTab(ticketid);
            return
        }

        state.layout.current.addTabToActiveTabSet({
            type: "tab",
            component: "GuacViewer",
            name: tabName,
            id: ticketid,
            config: {
                wspath: "/tunnelws/ticket/" + ticketid + "/",
                focused: true,
                tabIndex: 1,
                controlInput: controlInput,
                controlSize: controlSize,
                // screenSize null - means "auto", otherwise object e.g. {witdh:1024, height:768}
                screenSize: null,
                nodeSelectCallback: (tabNodeId) => {
                    state.model.doAction(FlexLayout.Actions.selectTab(tabNodeId));
                },
                nodeDeleteCallback: (tabNodeId) => {
                    state.model.doAction(FlexLayout.Actions.deleteTab(tabNodeId));
                }
            }
        });
    };

    const updateTabScreenSize = (tabid, screenSize) => {
        let config = state.model.getNodeById(tabid).getConfig();
        state.model.doAction(FlexLayout.Actions.updateNodeAttributes(tabid, {
            config: {
                ...config,
                screenSize: screenSize
            }
        }));
    };

    const addIframeTab = ({url, name}) => {
        state.layout.current.addTabToActiveTabSet({
            type: "tab",
            component: "TabIframe",
            name: name,
            config: {
                url: url
            }
        });
    };

    const defaultState = {
        model: FlexLayout.Model.fromJson(defaultLayout),
        layout: layoutRef,
        actions: {
            activateTicket: activateTicket,
            addIframeTab: addIframeTab,
            deleteTab: deleteTab,
            refreshTab: refreshTab,
            updateTabScreenSize: updateTabScreenSize,
        }
    };

    const [state, setState] = useState(defaultState);

    // when we have our layout, focus on main tabset
    // this will alow to use state.layout.current.addTabToActiveTabSet()
    useEffect(() => {
        if (state.model) {
            state.model.doAction(FlexLayout.Actions.setActiveTabset("tabset_main"))
        }
    }, [state.model]);

    return (
        <LayoutContext.Provider value={[state, setState]}>
            {props.children}
        </LayoutContext.Provider>
    );
};

export {LayoutContext, LayoutProvider};