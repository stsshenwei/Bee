from __future__ import annotations

import json
import uuid

from app.services.storage.postgres import PostgresDatabase, quote_ident

from .models import SkillActivation, SkillPackage, SkillVersion


class PostgresSkillRepository:
    def __init__(self, database: PostgresDatabase):
        self.database = database
        self.schema = quote_ident(database.settings.schema)

    def initialize_schema(self) -> None:
        sql = f"""
        CREATE TABLE IF NOT EXISTS {self.schema}.skill_package (
          id text PRIMARY KEY, owner_id text NOT NULL, name text NOT NULL,
          description text NOT NULL, category text NOT NULL DEFAULT '', author text NOT NULL DEFAULT '',
          tags jsonb NOT NULL DEFAULT '[]', license text NOT NULL DEFAULT '', source_url text NOT NULL DEFAULT '',
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(), UNIQUE(owner_id,name));
        CREATE TABLE IF NOT EXISTS {self.schema}.skill_version (
          id text PRIMARY KEY, skill_id text NOT NULL REFERENCES {self.schema}.skill_package(id), version text NOT NULL,
          status text NOT NULL CHECK(status IN ('published','yanked')), manifest_json jsonb NOT NULL,
          skill_markdown text NOT NULL, file_index_json jsonb NOT NULL, blob_key text NOT NULL, sha256 text NOT NULL,
          size bigint NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(skill_id,version));
        CREATE TABLE IF NOT EXISTS {self.schema}.workspace_skill_activation (
          workspace_id text NOT NULL, skill_id text NOT NULL REFERENCES {self.schema}.skill_package(id),
          version_id text NOT NULL REFERENCES {self.schema}.skill_version(id), enabled boolean NOT NULL,
          updated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(workspace_id,skill_id));
        """
        with self.database.transaction() as conn:
            conn.execute(sql)

    def _one(self, sql, params=()):
        with self.database.connection() as conn: return conn.execute(sql, params).fetchone()
    def _write(self, sql, params=()):
        with self.database.transaction() as conn: return conn.execute(sql, params).fetchone()
    def _all(self, sql, params=()):
        with self.database.connection() as conn: return conn.execute(sql, params).fetchall()
    def create_package(self, **v):
        sid=v.pop("id",uuid.uuid4().hex)
        row=self._write(f"INSERT INTO {self.schema}.skill_package(id,owner_id,name,description,category,author,tags,license,source_url) VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s) RETURNING *",(sid,v['owner'],v['name'],v['description'],v.get('category',''),v.get('author',''),json.dumps(v.get('tags',())),v.get('license',''),v.get('source_url','')))
        return self._package(row)
    def get_package(self,sid): return self._package(self._one(f"SELECT * FROM {self.schema}.skill_package WHERE id=%s",(sid,)))
    def find_package(self,owner,name): return self._package(self._one(f"SELECT * FROM {self.schema}.skill_package WHERE owner_id=%s AND name=%s",(owner,name)))
    def list_packages(self): return [self._package(r) for r in self._all(f"SELECT * FROM {self.schema}.skill_package ORDER BY updated_at DESC")]
    def add_version(self,**v):
        vid=uuid.uuid4().hex
        row=self._write(f"INSERT INTO {self.schema}.skill_version(id,skill_id,version,status,manifest_json,skill_markdown,file_index_json,blob_key,sha256,size) VALUES(%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,%s,%s) RETURNING *",(vid,v['skill_id'],v['version'],v['status'],json.dumps(v['metadata']),v['markdown'],json.dumps(v['files']),v['blob_key'],v['sha256'],v['size']))
        return self._version(row)
    def get_version(self,sid,version): return self._version(self._one(f"SELECT * FROM {self.schema}.skill_version WHERE skill_id=%s AND version=%s",(sid,version)))
    def list_versions(self,sid): return [self._version(r) for r in self._all(f"SELECT * FROM {self.schema}.skill_version WHERE skill_id=%s ORDER BY created_at DESC",(sid,))]
    def set_version_status(self,sid,version,status): return self._version(self._write(f"UPDATE {self.schema}.skill_version SET status=%s WHERE skill_id=%s AND version=%s RETURNING *",(status,sid,version)))
    def set_activation(self,wid,sid,version,enabled):
        row=self._write(f"INSERT INTO {self.schema}.workspace_skill_activation(workspace_id,skill_id,version_id,enabled) SELECT %s,%s,id,%s FROM {self.schema}.skill_version WHERE skill_id=%s AND version=%s ON CONFLICT(workspace_id,skill_id) DO UPDATE SET version_id=excluded.version_id,enabled=excluded.enabled,updated_at=now() RETURNING workspace_id,skill_id,enabled,updated_at,(SELECT version FROM {self.schema}.skill_version WHERE id=version_id) version",(wid,sid,enabled,sid,version)); return self._activation(row)
    def get_activation(self,wid,sid): return self._activation(self._one(f"SELECT a.workspace_id,a.skill_id,a.enabled,a.updated_at,v.version FROM {self.schema}.workspace_skill_activation a JOIN {self.schema}.skill_version v ON v.id=a.version_id WHERE a.workspace_id=%s AND a.skill_id=%s",(wid,sid)))
    def list_activations(self,wid): return [self._activation(r) for r in self._all(f"SELECT a.workspace_id,a.skill_id,a.enabled,a.updated_at,v.version FROM {self.schema}.workspace_skill_activation a JOIN {self.schema}.skill_version v ON v.id=a.version_id WHERE a.workspace_id=%s",(wid,))]
    @staticmethod
    def _package(r): return None if not r else SkillPackage(str(r['id']),str(r['owner_id']),str(r['name']),str(r['description']),str(r['category']),str(r['author']),tuple(r['tags']),str(r['license']),str(r['source_url']),str(r['created_at']),str(r['updated_at']))
    @staticmethod
    def _version(r): return None if not r else SkillVersion(str(r['id']),str(r['skill_id']),str(r['version']),str(r['status']),dict(r['manifest_json']),str(r['skill_markdown']),tuple(r['file_index_json']),str(r['blob_key']),str(r['sha256']),int(r['size']),str(r['created_at']))
    @staticmethod
    def _activation(r): return None if not r else SkillActivation(str(r['workspace_id']),str(r['skill_id']),str(r['version']),bool(r['enabled']),str(r['updated_at']))
