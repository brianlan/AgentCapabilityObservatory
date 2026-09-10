# Separate management and session surfaces

An evaluated session is untrusted but needs network access to fetch one bound task and request submission, while operators need broader controls. A path distinction inside one reachable application does not enforce that trust separation.

ACO therefore exposes two ASGI interfaces backed by the same domain modules and SQLite database. The authenticated Management Surface is not exposed to evaluated containers; the Session Surface exposes only trial-scoped task, finish, and status operations. A session capability expires when its Trial finishes. Separate listeners or container networks reinforce authorization without splitting the local control plane into services.
