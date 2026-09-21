"""Back up a disposable Compose PostgreSQL test DB and restore it into another test DB."""

import argparse
import subprocess
import tempfile
import uuid
from pathlib import Path

TABLES = [
    "users",
    "profiles",
    "doctors",
    "availability_slots",
    "consultations",
    "prescriptions",
    "payments",
    "audit_logs",
    "idempotency_records",
]


def docker_db(*args: str, input_data: bytes | None = None) -> bytes:
    command = ["docker", "compose", "exec", "-T", "db", *args]
    result = subprocess.run(command, input=input_data, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(f"{' '.join(command)} failed: {result.stderr.decode(errors='replace')}")
    return result.stdout


def counts(database: str) -> list[str]:
    query = "SELECT version_num FROM alembic_version;" + "".join(
        f"SELECT count(*) FROM {table};" for table in TABLES
    )
    output = docker_db("psql", "-U", "amrutam", "-d", database, "-Atc", query)
    return output.decode().splitlines()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="amrutam_test")
    args = parser.parse_args()
    if not args.source.endswith("_test") or not args.source.replace("_", "").isalnum():
        parser.error("Source must be a simple disposable database name ending in _test")
    restored = f"amrutam_restore_{uuid.uuid4().hex[:10]}_test"
    with tempfile.TemporaryDirectory(prefix="amrutam-backup-drill-") as temp:
        backup = Path(temp) / "backup.dump"
        backup.write_bytes(docker_db("pg_dump", "-U", "amrutam", "-Fc", "-d", args.source))
        if backup.stat().st_size == 0:
            raise RuntimeError("pg_dump created an empty file")
        docker_db("createdb", "-U", "amrutam", restored)
        try:
            docker_db(
                "pg_restore",
                "-U",
                "amrutam",
                "--no-owner",
                "--no-acl",
                "-d",
                restored,
                input_data=backup.read_bytes(),
            )
            source_counts, restored_counts = counts(args.source), counts(restored)
            if source_counts != restored_counts:
                raise RuntimeError("Restored Alembic version/table counts differ from source")
            print(
                f"BACKUP/RESTORE PASS: {backup.stat().st_size} bytes; "
                f"revision {restored_counts[0]}; {len(TABLES)} table counts match"
            )
        finally:
            docker_db("dropdb", "-U", "amrutam", "--if-exists", restored)
    print("Temporary restored database and backup file removed. This is not a PITR test.")


if __name__ == "__main__":
    main()
