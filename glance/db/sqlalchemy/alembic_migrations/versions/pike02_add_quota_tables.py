# Copyright (c) 2017 Sberbank
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""Add quota and reservation tables

Revision ID: pike02
Revises: pike01
Create Date: 2017-07-07 07:07:07.070707

"""
import datetime

from alembic import op
from oslo_utils import uuidutils
import sqlalchemy
from sqlalchemy import and_
from sqlalchemy import schema as sa_schema

from glance.api.v2 import quotas
from glance.common import timeutils
from glance.db.sqlalchemy.migrate_repo import schema


# revision identifiers, used by Alembic.
revision = 'pike02'
down_revision = 'pike01'
branch_labels = None
depends_on = None


def _add_reservations_for_existing_images():
    connection = op.get_bind()
    images_table = sqlalchemy.Table(
        'images', sqlalchemy.MetaData(),
        autoload=True, autoload_with=connection)
    properties_table = sqlalchemy.Table(
        'image_properties', sqlalchemy.MetaData(),
        autoload=True, autoload_with=connection)
    reservations_table = sqlalchemy.Table(
        'reservations', sqlalchemy.MetaData(),
        autoload=True, autoload_with=connection)

    # First create reservations for all existing images
    session = sqlalchemy.orm.Session(bind=connection.connect())
    records = session.query(images_table).filter(
        images_table.c.status == 'active').all()

    reservations_data = []
    for record in records:
        # Add image quantity quota
        reservations_data.append({
            'id': uuidutils.generate_uuid(),
            'created_at': timeutils.utcnow(),
            'reserved': 1,
            'quota_class': 'total_images_number',
            'scope': record.owner,
            'expire': None,
            'image_id': record.id,
            'deleted': False,
        })
        # Add overall image size quota
        if record.size:
            reservations_data.append({
                'id': uuidutils.generate_uuid(),
                'created_at': timeutils.utcnow(),
                'reserved': record.size,
                'quota_class': 'total_images_size',
                'scope': record.owner,
                'expire': None,
                'image_id': record.id,
                'deleted': False,
            })
    op.bulk_insert(reservations_table, reservations_data)

    # Second create reservations for snapshots
    snapshot_reservations_data = []
    records = session.query(
        images_table.outerjoin(
            properties_table,
            and_(images_table.c.id == properties_table.c.image_id,
                 properties_table.c.name == 'image_type')
        )
    ).filter(
        images_table.c.status == 'active'
    ).filter(
        properties_table.c.value == 'snapshot'
    ).all()

    for record in records:
        # Add image snapshot quantity quota
        snapshot_reservations_data.append({
            'id': uuidutils.generate_uuid(),
            'created_at': timeutils.utcnow(),
            'reserved': 1,
            'quota_class': 'total_snapshots_number',
            'scope': record.owner,
            'expire': None,
            'image_id': record.id,
            'deleted': False,
        })
        # Add overall image snapshot size quota
        if record.size:
            snapshot_reservations_data.append({
                'id': uuidutils.generate_uuid(),
                'created_at': timeutils.utcnow(),
                'reserved': record.size,
                'quota_class': 'total_snapshots_size',
                'scope': record.owner,
                'expire': None,
                'image_id': record.id,
                'deleted': False,
            })
    op.bulk_insert(reservations_table, snapshot_reservations_data)
    session.commit()
    session.close_all()


def create_quota_classes_table():
    quota_classes = op.create_table(
        'quota_classes',
        sa_schema.Column(
            'name', schema.String(255), primary_key=True, nullable=False),
        sa_schema.Column('default_limit', schema.BigInteger(), nullable=False),
        sa_schema.Column(
            'created_at', schema.DateTime(), nullable=False,
            default=lambda: timeutils.utcnow()),
        sa_schema.Column(
            'updated_at', schema.DateTime(),
            default=lambda: timeutils.utcnow()),
        sa_schema.Column('deleted_at', schema.DateTime()),
        sa_schema.Column(
            'deleted', schema.Boolean(), nullable=False, default=False),
        mysql_engine='InnoDB',
        mysql_charset='utf8')

    quota_class_data = []
    for name in quotas.quota_classes:
        quota_class_data.append({'name': name, 'default_limit': -1})

    op.bulk_insert(quota_classes, quota_class_data)


def create_quotas_table():
    op.create_table(
        'quotas',
        sa_schema.Column(
            'id', schema.String(36), primary_key=True,
            default=lambda: uuidutils.generate_uuid()),
        sa_schema.Column('scope', schema.String(255), nullable=False),
        sa_schema.Column(
            'quota_class', schema.String(255),
            sa_schema.ForeignKey('quota_classes.name'), nullable=False),
        sa_schema.Column('hard_limit', schema.BigInteger(), nullable=False),
        sa_schema.Column(
            'created_at', schema.DateTime(), nullable=False,
            default=lambda: timeutils.utcnow()),
        sa_schema.Column(
            'updated_at', schema.DateTime(),
            default=lambda: timeutils.utcnow()),
        sa_schema.Column('deleted_at', schema.DateTime()),
        sa_schema.Column(
            'deleted', schema.Boolean(), nullable=False, default=False),
        sa_schema.UniqueConstraint(
            'scope', 'quota_class', name="quota_must_be_unique_per_scope"),
        sa_schema.Index('ix_quota_scope', "scope"),
        sa_schema.Index('ix_quota_class', 'quota_class'),
        mysql_engine='InnoDB',
        mysql_charset='utf8')


def create_reservations_table():
    op.create_table(
        'reservations',
        sa_schema.Column(
            'id', schema.String(36), primary_key=True, nullable=False,
            default=(lambda: uuidutils.generate_uuid())),
        sa_schema.Column(
            'reserved', schema.BigInteger(), nullable=False, default=0),
        sa_schema.Column(
            'quota_class', schema.String(255),
            sa_schema.ForeignKey('quota_classes.name'), nullable=False),
        sa_schema.Column('scope', schema.String(255), nullable=False),
        sa_schema.Column(
            'expire', schema.DateTime(),
            default=lambda: timeutils.utcnow() + datetime.timedelta(hours=5)),
        sa_schema.Column('image_id', schema.String(36), nullable=False),
        sa_schema.Column(
            'created_at', schema.DateTime(), nullable=False,
            default=lambda: timeutils.utcnow()),
        sa_schema.Column(
            'updated_at', schema.DateTime(),
            default=lambda: timeutils.utcnow()),
        sa_schema.Column('deleted_at', schema.DateTime()),
        sa_schema.Column(
            'deleted', schema.Boolean(), nullable=False, default=False),
        sa_schema.UniqueConstraint(
            'quota_class', 'image_id', 'scope', 'expire',
            name='quota_unique_per_image'),
        sa_schema.Index('ix_reservation_class', 'quota_class'),
        sa_schema.Index('ix_reservation_image', 'image_id'),
        sa_schema.Index('ix_reservation_scope', 'scope'),
        sa_schema.Index('ix_reservation_expire', 'expire'),
        mysql_engine='InnoDB',
        mysql_charset='utf8')


def upgrade():
    create_quota_classes_table()
    create_quotas_table()
    create_reservations_table()
    _add_reservations_for_existing_images()
