import aiosqlite
from pathlib import Path
from models import TrackInfo, Source

class DatabaseService:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    async def init_db(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                CREATE TABLE IF NOT EXISTS track_cache (
                    identifier TEXT PRIMARY KEY,
                    title TEXT,
                    uploader TEXT,
                    duration INTEGER,
                    thumbnail_url TEXT,
                    source TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            await db.commit()

    async def get_track(self, identifier: str) -> Optional[TrackInfo]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute('SELECT * FROM track_cache WHERE identifier = ?', (identifier,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    return TrackInfo(identifier=row[0], title=row[1], uploader=row[2], duration=row[3], thumbnail_url=row[4], source=Source(row[5]))
        return None

    async def save_track(self, track: TrackInfo):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                INSERT OR REPLACE INTO track_cache (identifier, title, uploader, duration, thumbnail_url, source)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (track.identifier, track.title, track.uploader, track.duration, track.thumbnail_url, track.source.value))
            await db.commit()
