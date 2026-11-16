import React, {useContext, useState} from 'react';
import {AppContext} from "../../Context/AppContext";


function Welcome() {
    const [appState,] = useContext(AppContext);
    const [tokenName, setTokenName] = useState("");
    const [creating, setCreating] = useState(false);
    const user = appState.user;
    const tickets = appState.tickets;
    const agentTokens = (appState.agentTokens || []).filter(token => !token.revoked_at);

    const createToken = (event) => {
        event.preventDefault();
        const name = tokenName.trim();
        if (!name || creating) {
            return;
        }
        setCreating(true);
        appState.actions.createAgentToken(name).then(() => {
            setTokenName("");
        }).finally(() => {
            setCreating(false);
        });
    };

    return (
        <div className="gc-welcome">
            <header className="gc-welcome-head">
                <div>
                    <p className="gc-kicker">Signed in</p>
                    <h1 className="gc-user-name">{user.first_name + " " + user.last_name + " (" + user.username + ")"}</h1>
                </div>
                <button type="button" className="gc-logout" onClick={appState.actions.logout}>Logout</button>
            </header>

            <section className="gc-panel">
                <h2 className="gc-kicker">Connections</h2>
                <dl className="gc-stats">
                    <div>
                        <dt>Total</dt>
                        <dd>{getConnectionsCount(appState.connections)}</dd>
                    </div>
                    <div>
                        <dt>RDP</dt>
                        <dd>{getConnectionsCount(appState.connections, "rdp")}</dd>
                    </div>
                    <div>
                        <dt>SSH</dt>
                        <dd>{getConnectionsCount(appState.connections, "ssh")}</dd>
                    </div>
                    <div>
                        <dt>VNC</dt>
                        <dd>{getConnectionsCount(appState.connections, "vnc")}</dd>
                    </div>
                </dl>
            </section>

            <section className="gc-panel">
                <h2 className="gc-kicker">Access tickets</h2>
                <dl className="gc-stats">
                    <div>
                        <dt>Personal</dt>
                        <dd>{tickets.filter(t => t.user.id === user.id).length}</dd>
                    </div>
                    <div>
                        <dt>Received</dt>
                        <dd>{tickets.filter(t => t.user.id === user.id && t.user.id !== t.author.id).length}</dd>
                    </div>
                    <div>
                        <dt>Shared</dt>
                        <dd>{tickets.filter(t => t.author.id === user.id && t.user.id !== t.author.id).length}</dd>
                    </div>
                </dl>
            </section>

            <section className="gc-panel">
                <h2 className="gc-kicker">Agent token</h2>
                <form className="gc-token-row" onSubmit={createToken}>
                    <input
                        type="text"
                        name="name"
                        aria-label="Name"
                        placeholder="Name"
                        maxLength={60}
                        autoComplete="off"
                        value={tokenName}
                        onChange={(event) => setTokenName(event.target.value)}
                    />
                    <button type="submit" className="gc-logout" disabled={creating || !tokenName.trim()}>Create token</button>
                </form>
                {appState.agentTokenSecret ? (
                    <p className="gc-token-secret">{appState.agentTokenSecret}</p>
                ) : null}
                {agentTokens.map(token => (
                    <div className="gc-token-row" key={token.id}>
                        <span>{token.name}</span>
                        <button
                            type="button"
                            className="gc-logout"
                            onClick={() => appState.actions.revokeAgentToken(token.id)}
                        >
                            Revoke
                        </button>
                    </div>
                ))}
            </section>

            <p className="gc-docs">
                <a target="_blank" rel="noreferrer" href="https://guacozy.readthedocs.io">Guacozy</a>
                {" "}{process.env.REACT_APP_VERSION}
            </p>
        </div>
    );
}

function getConnectionsCount(tree, protocol) {
    let filter = () => true;

    if (protocol) {
        filter = (node) => node.protocol === protocol;
    }

    if (tree) {
        return tree.reduce(function (total, node) {
            return total + ((!node.isFolder && filter(node)) ? 1 : 0) + getConnectionsCount(node.children, protocol);
        }, 0)
    }
    return 0;
}

export default Welcome;