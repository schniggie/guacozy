import React, {useContext} from 'react';
import {AppContext} from "../../../Context/AppContext";
import {Container, Divider, Segment} from "../../../ui/ui";
import TabLink from "../TabLink/TabLink";
import {SETTINGS_LINKS} from "../../../settings"

function SettingsSidebar(props) {
    const [appState,] = useContext(AppContext);

    return (
        <Container className='sidebarContainer gc-settings'>
            <Segment color='grey'>
                {SETTINGS_LINKS.map(link =>
                    <>
                        <TabLink
                            url={link.url}
                            tabName={link.tabName}
                            linkName={link.name}
                            visible={appState.user.is_staff}/>
                        <Divider/>
                    </>
                )}
            </Segment>
        </Container>
    );
}

export default SettingsSidebar;