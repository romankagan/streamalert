"""
Copyright 2017-present, Airbnb Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
import json
import os
import shutil
import tempfile
from abc import abstractmethod

from botocore.exceptions import ClientError

from stream_alert.shared.helpers.aws_api_client import AwsKms, AwsS3
from stream_alert.shared.logger import get_logger

LOGGER = get_logger(__name__)


class OutputCredentialsProvider(object):
    """Loads credentials that are housed on AWS S3, or cached locally.

    Helper service to OutputDispatcher.

    OutputDispatcher implementations may require credentials to authenticate with an external
    gateway. All credentials for OutputDispatchers are to be stored in a single bucket on AWS S3
    and are encrypted with AWS KMS. When alerts are dispatched via OutputDispatchers, these
    encrypted credentials are downloaded and cached locally on the filesystem. Then, AWS KMS is
    used to decrypt the credentials when in use.

    Public methods:
        load_credentials: Returns a dict of the credentials requested
        get_local_credentials_temp_dir(): Returns full path to a temporary directory where all
            encrypted credentials are cached.
    """

    def __init__(self,
                 service_name,
                 config=None,
                 defaults=None,
                 region=None,
                 prefix=None,
                 aws_account_id=None):
        self._service_name = service_name

        # Region: Check constructor args first, then config
        self._region = config['global']['account']['region'] if region is None else region

        # Prefix: Check constructor args first, then ENV, then config
        self._prefix = self._calculate_prefix(prefix, config)

        # Account Id: Check constructor args first, then ENV, then config
        self._account_id = self._calculate_account_id(aws_account_id, config)

        self._defaults = defaults if defaults else {}

        # Drivers are strategies utilized by this class for fetching credentials from various
        # locations on disk or remotely
        self._drivers = []  # type: list[CredentialsProvidingDriver]
        self._core_driver = None  # type: S3Driver
        self._setup_drivers()

    @staticmethod
    def _calculate_prefix(given_prefix, config):
        if given_prefix is not None:
            return given_prefix

        if 'STREAMALERT_PREFIX' in os.environ:
            return os.environ['STREAMALERT_PREFIX']

        return config['global']['account']['prefix']

    @staticmethod
    def _calculate_account_id(given_account_id, config):
        if given_account_id is not None:
            return given_account_id

        if 'AWS_ACCOUNT_ID' in os.environ:
            return os.environ['AWS_ACCOUNT_ID']

        return config['global']['account']['aws_account_id']

    def _setup_drivers(self):
        """Initializes all drivers.

        The Drivers are sequentially checked in the order they are appended to the driver list.
        """

        # Ephemeral driver
        ep_driver = EphemeralUnencryptedDriver(self._service_name)
        self._drivers.append(ep_driver)

        # Fall back onto downloading encrypted credentials from S3
        s3_driver = S3Driver(self._prefix, self._service_name, self._region, cache_driver=ep_driver)
        self._core_driver = s3_driver
        self._drivers.append(s3_driver)

    def save_credentials(self, descriptor, kms_key_alias, props):
        """Saves given credentials into S3.

        Args:
            descriptor (str): OutputDispatcher descriptor
            kms_key_alias (str): KMS Key alias provided by configs
            props (Dict(str, OutputProperty)): A dict containing strings mapped to OutputProperty
                objects.

        Returns:
            bool: True is credentials successfully saved. False otherwise.
        """

        creds = {name: prop.value
                 for (name, prop) in props.iteritems() if prop.cred_requirement}

        credentials = Credentials(creds, False, self._region)
        return self._core_driver.save_credentials_into_s3(descriptor, credentials, kms_key_alias)

    def load_credentials(self, descriptor):
        """Loads credentials from the drivers.

        Args:
            descriptor (str): unique identifier used to look up these credentials

        Returns:
            dict: the loaded credential info needed for sending alerts to this service
            or None if nothing gets loaded
        """
        credentials = None
        for driver in self._drivers:
            if driver.has_credentials(descriptor):
                credentials = driver.load_credentials(descriptor)
                if credentials:
                    break

        if not credentials:
            LOGGER.error(
                'All drivers failed to retrieve credentials for [%s.%s]',
                self._service_name,
                descriptor
            )
            return None
        elif credentials.is_encrypted():
            decrypted_creds = credentials.get_data_kms_decrypted()
        else:
            decrypted_creds = credentials.data()

        creds_dict = json.loads(decrypted_creds)

        # Add any of the hard-coded default output props to this dict (ie: url)
        defaults = self._defaults
        if defaults:
            creds_dict.update(defaults)

        return creds_dict

    def get_aws_account_id(self):
        """Returns the AWS account ID"""
        return self._account_id


class Credentials(object):
    """Encapsulation for a set of credentials.

    When storing to or loading from a Driver, the raw credentials data may or may not be encrypted
    (e.g. when writing to disk, we should always keep it encrypted). To allow Credentials to be
    passed from Driver to Driver without excessive calls to KMS.encrypt/decrypt, this "data" in
    the Credentials can be either encrypted or not. It is up to the code that constructs the
    Credentials object to know which.

    When retrieving the raw, unencrypted data from a Credentials object, use the following code:

        if credentials.is_encrypted():
            return credentials.get_data_kms_decrypted()
        else:
            return credentials.data()
    """

    def __init__(self, data, is_encrypted=False, region=None):
        """
        Args:
            data (object|string): A json serializable object, or a string.
            is_encrypted (bool): Pass True if the input data is encrypted with KMS. False otherwise.
            region (str): AWS Region. Only required if is_encrypted=True.
        """
        self._data = data
        self._is_encrypted = is_encrypted
        self._region = region if is_encrypted else None  # No use for region if unencrypted

    def is_encrypted(self):
        """True if this Credentials object is encrypted. False otherwise.

        Returns:
            bool
        """
        return self._is_encrypted

    def data(self):
        """
        Returns:
            str: The raw text data of this Credentials object, encrypted or not. This may be
                unusable if encrypted, but can be passed to another Driver for storage.
        """
        return self._data

    def get_data_kms_decrypted(self):
        """Returns the data of this Credentials objects, decrypted with KMS.

        This does not mutate the internals of this Credentials for safety. It simply returns
        the decrypted payload. It is up to the called to safely manage the payload.

        Returns:
            str|None: The decrypted payload of this Credentials object, if it is encrypted. If it is
                not encrypted, then will return None and log an error.
        """
        if not self._is_encrypted:
            LOGGER.error('Cannot decrypt Credentials as they are already decrypted')
            return None

        try:
            return AwsKms.decrypt(self._data, region=self._region)
        except ClientError:
            LOGGER.exception('an error occurred during credentials decryption')
            return None

    def encrypt(self, region, kms_key_alias):
        """Encrypts the current Credentials.

        Calling this method will entirely change the internals of this Credentials object.
        Subsequent calls to .data() will return encrypted data.
        """
        if self.is_encrypted():
            return

        self._is_encrypted = True
        if not self._data:
            return

        creds_json = json.dumps(self._data, separators=(',', ':'))
        self._region = region
        self._data = AwsKms.encrypt(creds_json, region=self._region, key_alias=kms_key_alias)


class CredentialsProvidingDriver(object):
    """Drivers encapsulate logic for loading credentials"""

    @abstractmethod
    def load_credentials(self, descriptor):
        """Loads the requested credentials into a new Credentials object.

        The behavior can be nondeterministic if has_credentials() is false.

        Args:
            descriptor (string): Descriptor for the current output service

        Return:
            Credentials|None: Returns None when loading fails.
        """

    @abstractmethod
    def has_credentials(self, descriptor):
        """Determines whether the current driver is capable of loading the requested credentials.

        Args:
            descriptor (string): Descriptor for the current output service

        Return:
            bool: True if this driver has the requested Credentials, False otherwise.
        """


class FileDescriptorProvider(object):
    """Interface for Drivers capable of offering file-handles to aid in download of credentials."""

    @abstractmethod
    def offer_fileobj(self, descriptor):
        """Offers a file-like object.

        The caller is expected to call this method in a with block, and this file-like object
        is expected to automatically close.

        Returns:
             file object
        """


class CredentialsCachingDriver(object):
    """Interface for Drivers capable of being used as a caching layer to accelerate the speed
    of credential loading."""

    @abstractmethod
    def save_credentials(self, descriptor, credentials):
        """Saves the given credentials.

        On a subsequent call of load_credentials(), the same credentials will be loaded.

        Args:
            descriptor (str): OutputDispatcher descriptor
            credentials (Credentials): The credentials object to save. Notably, certain drivers are
                incapable of (or disallowed from) saving credentials that are unencrypted.

        Return:
            bool: True if saving succeeds. False otherwise.
        """


def get_formatted_output_credentials_name(service_name, descriptor):
    """Gives a unique name for credentials for the given service + descriptor.

    Args:
        service_name (str): Service name on output class (i.e. "pagerduty", "demisto")
        descriptor (str): Service destination (ie: slack channel, pd integration)

    Returns:
        str: Formatted credential name (ie: slack/ryandchannel)
    """
    cred_name = str(service_name)

    # should descriptor be enforced in all rules?
    if descriptor:
        cred_name = '{}/{}'.format(cred_name, descriptor)

    return cred_name


class S3Driver(CredentialsProvidingDriver):
    """Driver for fetching credentials from AWS S3"""

    def __init__(self, prefix, service_name, region, file_driver=None, cache_driver=None):
        """
        Args:
            prefix (str): StreamAlert account prefix in configs
            service_name (str): The service name for the OutputDispatcher using this
            region (str): AWS Region
            file_driver (FileDescriptorProvider|None):
                Optional. When provided, the file_driver will be used to provide a File handle
                for downloading the S3 credentials into. This can be useful if it is desired to
                download the S3 credentials into a specific file for examination.

                If omitted, will defaulted to using SpooledTempfileDriver, which downloads
                the S3 file into memory temporarily, and is cleaned up afterward.

                In all cases, the credentials file is downloaded and stored in the file-like
                handle in ENCRYPTED FORM.

            cache_driver (CredentialsProvidingDriver|None):
                Optional. When provided, the downloaded credentials will be cached in the given
                driver. This is useful for reducing the number of S3/KMS calls and speeding up the
                system.

                (!) Storage encryption of the credentials is determined by the driver.
        """
        self._service_name = service_name
        self._region = region
        self._prefix = prefix
        self._bucket = self.get_s3_secrets_bucket()

        self._file_driver = file_driver  # type: FileDescriptorProvider
        if not self._file_driver:
            self._file_driver = SpooledTempfileDriver(self._service_name, self._region)

        self._cache_driver = cache_driver  # type: CredentialsCachingDriver

    def load_credentials(self, descriptor):
        """Loads credentials from AWS S3.

        Args:
            descriptor (str): Service destination (ie: slack channel, pd integration)

        Returns:
            Credentials: The loaded Credentials. None on failure
        """
        try:
            with self._file_driver.offer_fileobj(descriptor) as file_handle:
                enc_creds = AwsS3.download_fileobj(
                    file_handle,
                    bucket=self._bucket,
                    region=self._region,
                    key=self.get_s3_key(descriptor)
                )

            credentials = Credentials(enc_creds, True, self._region)
            if self._cache_driver:
                self._cache_driver.save_credentials(descriptor, credentials)

            return credentials
        except ClientError:
            LOGGER.exception('credentials for \'%s\' could not be downloaded from S3',
                             get_formatted_output_credentials_name(self._service_name, descriptor))
            return None

    def has_credentials(self, descriptor):
        """Always returns True, as S3 is the place where all encrypted credentials are
           guaranteed to be cold-stored."""
        return True

    def save_credentials_into_s3(self, descriptor, credentials, kms_key_alias):
        """Takes the given credentials, encrypts them, and saves them to AWS S3.

        Notably, this implementation is NOT for the CredentialsCachingDriver interface, as the
        S3Driver is not a caching driver.

        Args:
            descriptor (str): Descriptor of the current Output
            credentials (Credentials): Credentials object to be saved into S3
            kms_key_alias (str): KMS key alias for streamalert secrets

        Returns:
            bool: True on success, False otherwise.
        """
        s3_key = get_formatted_output_credentials_name(self._service_name, descriptor)

        # Encrypt the creds and push them to S3
        if not credentials.is_encrypted():
            credentials.encrypt(self._region, kms_key_alias)

        encrypted_credentials = credentials.data()
        if not encrypted_credentials:
            return True

        try:
            return AwsS3.put_object(
                encrypted_credentials,
                bucket=self._bucket,
                key=s3_key,
                region=self._region
            )
        except ClientError:
            LOGGER.exception(
                'An error occurred while sending credentials to S3 for key \'%s\' in bucket \'%s\'',
                s3_key,
                self._bucket
            )
            return False

    def get_s3_key(self, descriptor):
        """Returns an appropriate S3 bucket key for credentials relevant to this Output.

        Args:
            descriptor (str): Descriptor of the current Output

        Returns:
            string
        """
        return get_formatted_output_credentials_name(self._service_name, descriptor)

    def get_s3_secrets_bucket(self):
        """Returns an appropriate S3 bucket for all credentials relevant to this driver.

        Returns:
            string
        """
        return '{}.streamalert.secrets'.format(self._prefix)


class LocalFileDriver(CredentialsProvidingDriver, FileDescriptorProvider, CredentialsCachingDriver):
    """Driver for fetching credentials that are saved locally on the filesystem."""

    def __init__(self, region, service_name):
        self._region = region
        self._service_name = service_name
        self._temp_dir = self.get_local_credentials_temp_dir()

    def load_credentials(self, descriptor):
        local_cred_location = self.get_file_path(descriptor)
        with open(local_cred_location, 'rb') as cred_file:
            encrypted_credentials = cred_file.read()

        return Credentials(encrypted_credentials, True, self._region)

    def has_credentials(self, descriptor):
        return os.path.exists(self.get_file_path(descriptor))

    def save_credentials(self, descriptor, credentials):
        if not credentials.is_encrypted():
            LOGGER.error('Error: Writing unencrypted credentials to disk is disallowed.')
            return False

        with self.offer_fileobj(descriptor) as file_handle:
            file_handle.write(credentials.data())
        return True

    @staticmethod
    def clear():
        """Removes the local secrets directory that may be left from previous runs"""
        secrets_dirtemp_dir = LocalFileDriver.get_local_credentials_temp_dir()

        # Check if the folder exists, and remove it if it does
        if os.path.isdir(secrets_dirtemp_dir):
            shutil.rmtree(secrets_dirtemp_dir)

    def offer_fileobj(self, descriptor):
        """Opens a file-like object and returns it.

        If you use the return value in a `with` statement block then the file descriptor
        will auto-close.

        Args:
            descriptor (str): Descriptor of the current Output

        Return:
            file object
        """
        file_path = self.get_file_path(descriptor)
        if not os.path.exists(file_path):
            os.makedirs(os.path.dirname(file_path))

        return open(file_path, 'a+b')  # read+write and in binary mode

    def get_file_path(self, descriptor):
        local_cred_location = os.path.join(
            self._temp_dir,
            get_formatted_output_credentials_name(self._service_name, descriptor)
        )
        return local_cred_location

    @staticmethod
    def get_local_credentials_temp_dir():
        """Returns a temporary directory on the filesystem to store encrypted credentials.

        Will automatically create the new directory if it does not exist.

        Returns:
            str: local path for stream_alert_secrets tmp directory
        """
        temp_dir = os.path.join(tempfile.gettempdir(), "stream_alert_secrets")

        # Check if this item exists as a file, and remove it if it does
        if os.path.isfile(temp_dir):
            os.remove(temp_dir)

        # Create the folder on disk to store the credentials temporarily
        if not os.path.exists(temp_dir):
            os.makedirs(temp_dir)

        return temp_dir


class SpooledTempfileDriver(CredentialsProvidingDriver, FileDescriptorProvider):
    """Driver for fetching credentials that are stored in memory in file-like objects."""

    SERVICE_SPOOLS = {}

    def __init__(self, service_name, region):
        self._service_name = service_name
        self._region = region

    def has_credentials(self, descriptor):
        key = self.get_spool_cache_key(descriptor)
        return key in type(self).SERVICE_SPOOLS

    def load_credentials(self, descriptor):
        """Loads the credentials from a temporary spool."""
        key = self.get_spool_cache_key(descriptor)
        if key not in type(self).SERVICE_SPOOLS:
            LOGGER.error(
                'SpooledTempfileDriver failed to load_credentials: Spool "%s" does not exist?',
                key
            )
            return None

        spool = type(self).SERVICE_SPOOLS[key]

        spool.seek(0)
        raw_data = spool.read()

        return Credentials(raw_data, True, self._region)

    def save_credentials(self, descriptor, credentials):
        """Saves the credentials into a temporary spool.

        Args:
            descriptor (str): Descriptor of the current Output
            credentials (Credentials): Credentials object that is intended to be saved

        Return:
            bool: True on success, False otherwise
        """
        # Always store unencrypted because it's in memory. Saves calls to KMS and it's safe
        # because other unrelated processes cannot read this memory (probably..)
        if not credentials.is_encrypted():
            LOGGER.error('Error: Writing unencrypted credentials to disk is disallowed.')
            return False

        raw_creds = credentials.data()

        spool = tempfile.SpooledTemporaryFile()
        spool.write(raw_creds)

        key = self.get_spool_cache_key(descriptor)
        type(self).SERVICE_SPOOLS[key] = spool

        return True

    @classmethod
    def clear(cls):
        """Clears all global spools.

        De-allocating the spools triggers garbage collection, which implicitly closes the
        file handles.
        """
        cls.SERVICE_SPOOLS = {}

    def offer_fileobj(self, descriptor):
        """Opens a file-like temporary file spool and returns it.

        If you use the return value in a `with` statement block then the file descriptor
        auto-close.

        NOTE: (!) This returns an ephemeral spool that is not attached to the caching mechanism
            in save_credentials() and load_credentials()

        Args:
            descriptor (str): Descriptor of the current Output

        Returns:
            file object
        """
        return tempfile.SpooledTemporaryFile(0, 'a+b')

    def get_spool_cache_key(self, descriptor):
        return '{}/{}'.format(self._service_name, descriptor)


class EphemeralUnencryptedDriver(CredentialsProvidingDriver, CredentialsCachingDriver):
    """Stores credentials UNENCRYPTED on the Python runtime stack.

    It is ephemeral and is only readable by the current Python process... hopefully.
    """

    CREDENTIALS_STORE = {}

    def __init__(self, service_name):
        self._service_name = service_name

    def has_credentials(self, descriptor):
        key = self.get_storage_key(descriptor)
        return key in type(self).CREDENTIALS_STORE

    def load_credentials(self, descriptor):
        key = self.get_storage_key(descriptor)
        if key not in type(self).CREDENTIALS_STORE:
            LOGGER.error(
                'EphemeralUnencryptedDriver failed to load_credentials: Key "%s" does not exist?',
                key
            )
            return None

        unencrypted_raw_creds = type(self).CREDENTIALS_STORE[key]

        return Credentials(unencrypted_raw_creds, False)

    def save_credentials(self, descriptor, credentials):
        """Saves the credentials into static python memory.

        Args:
            descriptor (str): Descriptor of the current Output
            credentials (Credentials): Credentials object that is intended to be saved

        Return:
            bool: True on success, False otherwise
        """
        if credentials.is_encrypted():
            unencrypted_raw_creds = credentials.get_data_kms_decrypted()
        else:
            unencrypted_raw_creds = credentials.data()

        key = self.get_storage_key(descriptor)
        type(self).CREDENTIALS_STORE[key] = unencrypted_raw_creds
        return True

    @classmethod
    def clear(cls):
        cls.CREDENTIALS_STORE.clear()

    def get_storage_key(self, descriptor):
        return '{}/{}'.format(self._service_name, descriptor)
