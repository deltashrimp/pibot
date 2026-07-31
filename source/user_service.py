"""User-level business logic: global bans, username mapping."""

from persistence import Persistence


class UserService:
    """Wraps persistence operations related to users and global bans."""

    def __init__(self, persistence: Persistence) -> None:
        self.persistence = persistence

    @property
    def banned_users(self) -> set[int]:
        return self.persistence.banned_users

    @property
    def username_map(self) -> dict[str, int]:
        return self.persistence.username_map

    def is_banned(self, user_id: int) -> bool:
        """Return True if the user has a global ban."""
        return self.persistence.is_user_banned(user_id)

    async def ban(self, user_id: int) -> None:
        """Add a global ban, committed immediately."""
        await self.persistence.add_global_ban(user_id)

    async def unban(self, user_id: int) -> None:
        """Remove a global ban, committed immediately."""
        await self.persistence.remove_global_ban(user_id)
