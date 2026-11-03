import { isRegisteredAppName } from "./format";

/**
 * An actor or contributor label from F5. An agent's label is the name its app registered, not a
 * verified identity (finding O1), so it carries the same qualifier as ConnectionsScreen.
 */
export function ActorName({ name }: { name: string }) {
  if (!isRegisteredAppName(name)) return <span className="actor">{name}</span>;
  return (
    <span className="actor">
      <span>{name}</span>{" "}
      <span className="qualifier" title="The assistant app's registered name, not a verified identity">
        (registered app name)
      </span>
    </span>
  );
}
