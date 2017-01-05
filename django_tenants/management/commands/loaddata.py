import gzip
import os
import zipfile

from django.conf import settings
from django.core import serializers
from django.core.management.base import CommandError
from django.core.management.commands.loaddata import Command as LoadDataCommand
from django.core.management.commands.loaddata import SingleZipReader
from django.core.management.color import no_style
from django.db import connections, router

import logging
logger = logging.getLogger(__name__)

try:
    import bz2
    has_bz2 = True
except ImportError:
    has_bz2 = False


class Command(LoadDataCommand):
    def _schema_exists(self, schema_name):
        cursor = connections[self.using].cursor()
        sql = 'SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_namespace WHERE LOWER(nspname) = LOWER(%s))'
        cursor.execute(sql, (schema_name, ))
        row = cursor.fetchone()
        if row:
            exists = row[0]
        else:
            exists = False
        return exists

    def _create_schema(self, schema_name):
        cursor = connections[self.using].cursor()
        if self._schema_exists(schema_name):
            return False
        cursor.execute('CREATE SCHEMA %s' % schema_name)

    def loaddata(self, fixture_labels):
        connection = connections[self.using]

        # Keep a count of the installed objects and fixtures
        self.fixture_count = 0
        self.loaded_object_count = 0
        self.fixture_object_count = 0
        self.models = set()

        self.serialization_formats = serializers.get_public_serializer_formats()
        # Forcing binary mode may be revisited after dropping Python 2 support (see #22399)
        self.compression_formats = {
            None: (open, 'rb'),
            'gz': (gzip.GzipFile, 'rb'),
            'zip': (SingleZipReader, 'r'),
        }
        if has_bz2:
            self.compression_formats['bz2'] = (bz2.BZ2File, 'r')

        # If fixtures / test data need to be in a particular schema, we need to
        # create the schema before loading fixtures that may use it. Because
        # fixtures are loaded before any "setUp" code is run, we do this here.
        if settings.TESTING and settings.TEST_SCHEMA and settings.TEST_SCHEMA != 'public':
            self._create_schema(settings.TEST_SCHEMA)

        # Django's test suite repeatedly tries to load initial_data fixtures
        # from apps that don't have any fixtures. Because disabling constraint
        # checks can be expensive on some database (especially MSSQL), bail
        # out early if no fixtures are found.
        for fixture_label in fixture_labels:
            if self.find_fixtures(fixture_label):
                break
        else:
            return

        with connection.constraint_checks_disabled():
            for fixture_label in fixture_labels:
                self.load_label(fixture_label)

        # Since we disabled constraint checks, we must manually check for
        # any invalid keys that might have been added
        table_names = [model._meta.db_table for model in self.models]
        try:
            connection.check_constraints(table_names=table_names)
        except Exception as e:
            e.args = ("Problem installing fixtures: %s" % e,)
            raise

        # If we found even one object in a fixture, we need to reset the
        # database sequences.
        if self.loaded_object_count > 0:
            sequence_sql = connection.ops.sequence_reset_sql(no_style(), self.models)
            if sequence_sql:
                if self.verbosity >= 2:
                    self.stdout.write("Resetting sequences\n")
                with connection.cursor() as cursor:
                    for line in sequence_sql:
                        cursor.execute(line)

        if self.verbosity >= 1:
            if self.fixture_count == 0 and self.hide_empty:
                pass
            elif self.fixture_object_count == self.loaded_object_count:
                self.stdout.write("Installed %d object(s) from %d fixture(s)" %
                    (self.loaded_object_count, self.fixture_count))
            else:
                self.stdout.write("Installed %d object(s) (of %d) from %d fixture(s)" %
                    (self.loaded_object_count, self.fixture_object_count, self.fixture_count))

    def load_label(self, fixture_label):
        """
        Loads fixtures files for a given label.
        """
        connection = connections[self.using]
        for fixture_file, fixture_dir, fixture_name in self.find_fixtures(fixture_label):
            _, ser_fmt, cmp_fmt = self.parse_name(os.path.basename(fixture_file))
            open_method, mode = self.compression_formats[cmp_fmt]
            fixture = open_method(fixture_file, mode)
            try:
                self.fixture_count += 1
                objects_in_fixture = 0
                loaded_objects_in_fixture = 0
                if self.verbosity >= 2:
                    self.stdout.write("Installing %s fixture '%s' from %s." %
                        (ser_fmt, fixture_name, humanize(fixture_dir)))

                objects = serializers.deserialize(ser_fmt, fixture,
                    using=self.using, ignorenonexistent=self.ignore)

                for obj in objects:
                    objects_in_fixture += 1
                    if router.allow_migrate_model(self.using, obj.object.__class__):
                        loaded_objects_in_fixture += 1
                        self.models.add(obj.object.__class__)

                        if settings.TESTING and settings.TEST_SCHEMA and obj.object._meta.app_label in settings.TENANT_APPS:
                            old_schema = (connection.tenant, connection.schema_name, connection.include_public_schema)
                            connection.set_schema(settings.TEST_SCHEMA)

                        try:
                            obj.save(using=self.using)
                        except (DatabaseError, IntegrityError) as e:
                            e.args = ("Could not load %(app_label)s.%(object_name)s(pk=%(pk)s): %(error_msg)s" % {
                                'app_label': obj.object._meta.app_label,
                                'object_name': obj.object._meta.object_name,
                                'pk': obj.object.pk,
                                'error_msg': force_text(e)
                            },)
                            raise

                        if settings.TESTING and settings.TEST_SCHEMA and old_schema:
                            (connection.tenant, connection.schema_name,
                             connection.include_public_schema) = old_schema
                            connection.set_settings_schema(connection.schema_name)

                self.loaded_object_count += loaded_objects_in_fixture
                self.fixture_object_count += objects_in_fixture
            except Exception as e:
                if not isinstance(e, CommandError):
                    e.args = ("Problem installing fixture '%s': %s" % (fixture_file, e),)
                raise
            finally:
                fixture.close()

            # Warn if the fixture we loaded contains 0 objects.
            if objects_in_fixture == 0:
                warnings.warn(
                    "No fixture data found for '%s'. (File format may be "
                    "invalid.)" % fixture_name,
                    RuntimeWarning
                )
