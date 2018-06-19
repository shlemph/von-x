#
# Copyright 2017-2018 Government of Canada
# Public Services and Procurement Canada - buyandsell.gc.ca
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import asyncio
import json
import logging
import pathlib
import random
import string
from typing import Mapping, Sequence
import uuid

import aiohttp
from didauth.indy import seed_to_did
from didauth.ext.aiohttp import SignedRequest, SignedRequestAuth
from von_agent.error import AbsentSchema, AbsentCredDef
from von_agent.nodepool import NodePool
from von_agent.util import cred_def_id, revealed_attrs, schema_id, schema_key

from ..common.service import (
    Exchange,
    ServiceBase,
    ServiceRequest,
    ServiceResponse,
)
from ..common.util import log_json
from .config import (
    AgentType,
    AgentCfg,
    ConnectionCfg,
    SchemaCfg,
    WalletCfg,
)
from .errors import IndyConfigError, IndyError
from .messages import (
    IndyServiceAck,
    IndyServiceFail,
    LedgerStatusReq,
    LedgerStatus,
    RegisterWalletReq,
    WalletStatusReq,
    WalletStatus,
    RegisterAgentReq,
    AgentStatusReq,
    AgentStatus,
    RegisterCredentialTypeReq,
    RegisterConnectionReq,
    ConnectionStatusReq,
    ConnectionStatus,
    IssueCredentialReq,
    Credential,
    CredentialOffer,
    CredentialRequest,
)

LOGGER = logging.getLogger(__name__)


def _make_id(pfx: str = '', length=12) -> str:
    return pfx + ''.join(random.choice(string.ascii_letters) for _ in range(length))


class IndyService(ServiceBase):
    """
    A class for managing interactions with the Hyperledger Indy ledger
    """

    def __init__(self, pid: str, exchange: Exchange, env: Mapping, spec: dict = None):
        super(IndyService, self).__init__(pid, exchange, env)
        self._config = {}
        self._genesis_path = None
        self._agents = {}
        self._connections = {}
        self._name = pid
        self._opened = False
        self._pool = None
        self._wallets = {}
        self._ledger_url = None
        self._verifier = None
        self._update_config(spec)

    def _update_config(self, spec) -> None:
        """
        Load configuration settings
        """
        if spec:
            self._config.update(spec)
        if "name" in spec:
            self._name = spec["name"]
        if "ledger_url" in spec:
            self._ledger_url = spec["ledger_url"]

    async def _service_sync(self) -> bool:
        """
        Perform the initial setup of the ledger connection, including downloading the
        genesis transaction file
        """
        await self._setup_pool()
        synced = True
        for wallet in self._wallets.values():
            if not wallet.created:
                await wallet.create(self._pool)
        for agent in self._agents.values():
            if not await self._sync_agent(agent):
                synced = False
        for connection in self._connections.values():
            if not await self._sync_connection(connection):
                synced = False
        return synced

    def _add_agent(self, agent_type: str, wallet_id: str, **params) -> str:
        """
        Add an agent configuration

        Args:
            agent_type: the agent type, issuer or holder
            wallet_id: the identifier for a previously-registered wallet
            params: parameters to be passed to the :class:`AgentCfg` constructor
        """
        if wallet_id not in self._wallets:
            raise IndyConfigError("Wallet ID not registered: {}".format(wallet_id))
        cfg = AgentCfg(agent_type, wallet_id, **params)
        if not cfg.agent_id:
            cfg.agent_id = _make_id("agent-")
        if cfg.agent_id in self._agents:
            raise IndyConfigError("Duplicate agent ID: {}".format(cfg.agent_id))
        self._agents[cfg.agent_id] = cfg
        return cfg.agent_id

    def _get_agent_status(self, agent_id: str) -> ServiceResponse:
        """
        Return the status of a registered agent

        Args:
            agent_id: the unique identifier of the agent
        """
        if agent_id in self._agents:
            msg = AgentStatus(agent_id, self._agents[agent_id].status)
        else:
            msg = IndyServiceFail("Unregistered agent: {}".format(agent_id))
        return msg

    def _add_credential_type(
            self,
            issuer_id: str,
            schema_name: str,
            schema_version: str,
            origin_did: str,
            attr_names: Sequence,
            config: Mapping = None) -> None:
        agent = self._agents[issuer_id]
        if not agent:
            raise IndyConfigError("Agent ID not registered: {}".format(issuer_id))
        schema = SchemaCfg(schema_name, schema_version, attr_names, origin_did)
        agent.add_credential_type(schema, **(config or {}))

    def _add_connection(self, connection_type: str, agent_id: str, **params) -> str:
        """
        Add a connection configuration

        Args:
            connection_type: the type of the connection, normally TheOrgBook
            agent_id: the identifier of the registered agent
            params: parameters to be passed to the :class:`ConnectionCfg` constructor
        """
        if agent_id not in self._agents:
            raise IndyConfigError("Agent ID not registered: {}".format(agent_id))
        cfg = ConnectionCfg(connection_type, agent_id, **params)
        if not cfg.connection_id:
            cfg.connection_id = _make_id("connection-")
        if cfg.connection_id in self._connections:
            raise IndyConfigError("Duplicate connection ID: {}".format(cfg.connection_id))
        self._connections[cfg.connection_id] = cfg
        return cfg.connection_id

    def _get_connection_status(self, connection_id: str) -> ServiceResponse:
        """
        Return the status of a registered connection

        Args:
            connection_id: the unique identifier of the connection
        """
        if connection_id in self._connections:
            msg = ConnectionStatus(connection_id, self._connections[connection_id].status)
        else:
            msg = IndyServiceFail("Unregistered connection: {}".format(connection_id))
        return msg

    def _add_wallet(self, **params) -> str:
        """
        Add a wallet configuration

        Args:
            params: parameters to be passed to the :class:`WalletCfg` constructor
        """
        cfg = WalletCfg(**params)
        if not cfg.wallet_id:
            cfg.wallet_id = _make_id("wallet-")
        if cfg.wallet_id in self._wallets:
            raise IndyConfigError("Duplicate wallet ID: {}".format(cfg.wallet_id))
        self._wallets[cfg.wallet_id] = cfg
        return cfg.wallet_id

    def _get_wallet_status(self, wallet_id: str) -> ServiceResponse:
        """
        Return the status of a registered wallet

        Args:
            wallet_id: the unique identifier of the wallet
        """
        if wallet_id in self._wallets:
            msg = WalletStatus(wallet_id, self._wallets[wallet_id].status)
        else:
            msg = IndyServiceFail("Unregistered wallet: {}".format(wallet_id))
        return msg

    async def _sync_agent(self, agent: AgentCfg) -> bool:
        """
        Perform agent synchronization, registering the DID and publishing schemas
        and credential definitions as required

        Args:
            agent: the Indy agent configuration
        """
        if not agent.synced:
            if not agent.created:
                wallet = self._wallets[agent.wallet_id]
                if not wallet.created:
                    return False
                await agent.create(wallet)

            await agent.open()

            if not agent.registered:
                # check DID is registered
                auto_register = self._config.get("auto_register", True)
                await self._check_registration(agent, auto_register, agent.role)

                # check endpoint is registered (if any)
                # await self._check_endpoint(agent.instance, agent.endpoint)
                agent.registered = True

            # publish schemas
            for cred_type in agent.cred_types:
                await self._publish_schema(agent, cred_type)

            agent.synced = True
            LOGGER.info("Indy agent synced: %s", agent.agent_id)
        return agent.synced

    async def _sync_connection(self, connection: ConnectionCfg) -> bool:
        agent = self._agents[connection.agent_id]

        if not connection.synced:
            if not connection.created:
                if not agent.synced:
                    return False
                agent_cfg = agent.get_connection_params(connection)
                await connection.create(agent_cfg)

            if not connection.opened:
                http_client = self._agent_http_client(agent.agent_id)
                await connection.open(http_client)

            await connection.sync(agent)
        return connection.synced

    async def _setup_pool(self) -> None:
        if not self._opened:
            await asyncio.sleep(1)  # help avoid odd TimeoutError on genesis txn retrieval
            await self._check_genesis_path()
            self._pool = NodePool(self._name, self._genesis_path)
            await self._pool.open()
            self._opened = True

    async def _check_genesis_path(self) -> None:
        """
        Make sure that the genesis path is defined, and download the transaction file if needed.
        """
        if not self._genesis_path:
            path = self._config.get("genesis_path")
            if not path:
                raise IndyConfigError("Missing genesis_path")
            genesis_path = pathlib.Path(path)
            if not genesis_path.exists():
                ledger_url = self._ledger_url
                if not ledger_url:
                    raise IndyConfigError(
                        "Cannot retrieve genesis transaction without ledger_url"
                    )
                parent_path = pathlib.Path(genesis_path.parent)
                if not parent_path.exists():
                    parent_path.mkdir(parents=True)
                await self._fetch_genesis_txn(ledger_url, genesis_path)
            elif genesis_path.is_dir():
                raise IndyConfigError("genesis_path must not point to a directory")
            self._genesis_path = path

    async def _fetch_genesis_txn(self, ledger_url: str, target_path: str) -> bool:
        """
        Download the genesis transaction file from the ledger server

        Args:
            ledger_url: the root address of the von-network ledger
            target_path: the filesystem path of the genesis transaction file once downloaded
        """
        LOGGER.info(
            "Fetching genesis transaction file from %s/genesis", ledger_url
        )

        try:
            async with aiohttp.ClientSession(read_timeout=30) as client:
                response = await client.get("{}/genesis".format(ledger_url))
        except aiohttp.ClientError as e:
            raise ServiceSyncError("Error downloading genesis transaction file: {}".format(str(e)))

        if response.status != 200:
            raise ServiceSyncError(
                "Error downloading genesis file: status {}".format(
                    response.status
                )
            )

        # check data is valid json
        data = await response.text()
        LOGGER.debug("Genesis transaction response: %s", data)
        lines = data.splitlines()
        if not lines or not json.loads(lines[0]):
            raise ServiceSyncError("Genesis transaction file is not valid JSON")

        # write result to provided path
        with target_path.open("x") as output_file:
            output_file.write(data)
        return True

    async def _check_registration(self, agent: AgentCfg, auto_register: bool = True,
                                  role: str = "") -> None:
        """
        Look up our nym on the ledger and register it if not present

        Args:
            agent: the initialized and opened agent to be checked
            auto_register: whether to automatically register the DID on the ledger
        """
        did = agent.did
        LOGGER.debug("Checking DID registration %s", did)
        nym_json = await agent.instance.get_nym(did)
        LOGGER.debug("get_nym result for %s: %s", did, nym_json)

        nym_info = json.loads(nym_json)
        if not nym_info:
            if not auto_register:
                raise ServiceSyncError(
                    "DID is not registered on the ledger and auto-registration disabled"
                )

            ledger_url = self._ledger_url
            if not ledger_url:
                raise IndyConfigError("Cannot register DID without ledger_url")
            LOGGER.info("Registering DID %s", did)

            async with aiohttp.ClientSession(read_timeout=30) as client:
                response = await client.post(
                    "{}/register".format(ledger_url),
                    json={"did": did, "verkey": agent.verkey, "role": role},
                )
                if response.status != 200:
                    raise ServiceSyncError(
                        "DID registration failed: {}".format(
                            await response.text()
                        )
                    )
                nym_info = await response.json()
                LOGGER.debug("Registration response: %s", nym_info)
                if not nym_info or not nym_info["did"]:
                    raise ServiceSyncError(
                        "DID registration failed: {}".format(nym_info)
                    )

    async def _check_endpoint(self, agent: AgentCfg, endpoint: str) -> None:
        """
        Look up our endpoint on the ledger and register it if not present

        Args:
            agent: the initialized and opened agent to be checked
            endpoint: the endpoint to be added to the ledger, if not defined
        """
        if not endpoint:
            return None
        did = agent.did
        LOGGER.debug("Checking endpoint registration %s", endpoint)
        endp_json = await agent.instance.get_endpoint(did)
        LOGGER.debug("get_endpoint result for %s: %s", did, endp_json)

        endp_info = json.loads(endp_json)
        if not endp_info:
            endp_info = await agent.instance.send_endpoint()
            LOGGER.debug("Endpoint stored: %s", endp_info)

    async def _publish_schema(self, issuer: AgentCfg, cred_type: dict) -> None:
        """
        Check the ledger for a specific schema and version, and publish it if not found.
        Also publish the related credential definition if not found

        Args:
            issuer: the initialized and opened issuer instance publishing the schema
            cred_type: a dict which will be updated with the published schema and credential def
        """

        if not cred_type or "definition" not in cred_type:
            raise IndyConfigError("Missing schema definition")
        definition = cred_type["definition"]

        if not cred_type.get("ledger_schema"):
            LOGGER.info(
                "Checking for schema: %s (%s)",
                definition.name,
                definition.version,
            )
            # Check if schema exists on ledger

            try:
                s_key = schema_key(
                    schema_id(issuer.did, definition.name, definition.version)
                )
                schema_json = await issuer.instance.get_schema(s_key)
                ledger_schema = json.loads(schema_json)
                log_json("Schema found on ledger:", ledger_schema, LOGGER)
            except AbsentSchema:
                # If not found, send the schema to the ledger
                LOGGER.info(
                    "Publishing schema: %s (%s)",
                    definition.name,
                    definition.version,
                )
                schema_json = await issuer.instance.send_schema(
                    json.dumps(
                        {
                            "name": definition.name,
                            "version": definition.version,
                            "attr_names": definition.attr_names,
                        }
                    )
                )
                ledger_schema = json.loads(schema_json)
                if not ledger_schema or not ledger_schema.get("seqNo"):
                    raise ServiceSyncError("Schema was not published to ledger")
                log_json("Published schema:", ledger_schema, LOGGER)
            cred_type["ledger_schema"] = ledger_schema

        if not cred_type.get("cred_def"):
            # Check if credential definition has been published
            LOGGER.info(
                "Checking for credential def: %s (%s)",
                definition.name,
                definition.version,
            )

            try:
                cred_def_json = await issuer.instance.get_cred_def(
                    cred_def_id(issuer.did, cred_type["ledger_schema"]["seqNo"])
                )
                cred_def = json.loads(cred_def_json)
                log_json("Credential def found on ledger:", cred_def, LOGGER)
            except AbsentCredDef:
                # If credential definition is not found then publish it
                LOGGER.info(
                    "Publishing credential def: %s (%s)",
                    definition.name,
                    definition.version,
                )
                cred_def_json = await issuer.instance.send_cred_def(
                    schema_json, revocation=False
                )
                cred_def = json.loads(cred_def_json)
                log_json("Published credential def:", cred_def, LOGGER)
            cred_type["cred_def"] = cred_def

    async def _issue_credential(
            self,
            connection_id: str,
            schema_name: str,
            schema_version: str,
            origin_did: str,
            cred_data: Mapping) -> ServiceResponse:
        """
        Issue a credential to the connection target
        """
        conn = self._connections.get(connection_id)
        if not conn:
            raise IndyConfigError("Unknown connection id: {}".format(connection_id))
        issuer = self._agents[conn.agent_id]
        if issuer.agent_type != AgentType.issuer:
            raise IndyConfigError("Cannot issue credential from non-issuer agent: {}".format(issuer.agent_id))
        if not issuer.synced:
            raise IndyConfigError("Issuer is not yet synchronized: {}".format(issuer.agent_id))
        cred_type = issuer.find_credential_type(schema_name, schema_version, origin_did)
        if not cred_type:
            raise IndyConfigError("Could not locate credential type: {}/{} {}".format(
                schema_name, schema_version, origin_did))

        cred_offer = await self._create_cred_offer(connection_id, issuer, cred_type)
        log_json("Created cred offer:", cred_offer, LOGGER)
        cred_request = await conn.instance.generate_credential_request(cred_offer)
        log_json("Got cred request:", cred_request, LOGGER)
        cred = await self._create_cred(issuer, cred_request, cred_data)
        log_json("Created cred:", cred, LOGGER)
        stored = await conn.instance.store_credential(cred)
        log_json("Stored credential:", stored, LOGGER)
        return stored

    async def _create_cred_offer(self, connection_id: str, issuer: AgentCfg, cred_type) -> CredentialOffer:
        schema = cred_type["definition"]

        LOGGER.info(
            "Creating Indy credential offer for issuer %s, schema %s",
            issuer.agent_id,
            schema.name,
        )
        cred_offer_json = await issuer.instance.create_cred_offer(
            cred_type["ledger_schema"]["seqNo"]
        )
        return CredentialOffer(
            connection_id,
            schema.name,
            schema.version,
            json.loads(cred_offer_json),
            cred_type["cred_def"],
        )

    async def _create_cred(
            self,
            issuer: AgentCfg,
            request: CredentialRequest,
            cred_data: Mapping) -> Credential:

        cred_offer = request.cred_offer
        (cred_json, cred_revoc_id) = await issuer.instance.create_cred(
            json.dumps(cred_offer.offer),
            request.data,
            cred_data,
        )
        return Credential(
            request.connection_id,
            cred_offer.schema_name,
            issuer.did,
            json.loads(cred_json),
            cred_offer.cred_def,
            request.metadata,
            cred_revoc_id,
        )

    async def _get_verifier(self) -> AgentCfg:
        """
        Fetch or create an :class:`AgentWrapper` representing a standard Verifier agent,
        used to verify proofs
        """
        if not self._verifier:
            wallet_cfg = self._wallets['_verifier'] = WalletCfg(
                name="GenericVerifier",
                seed="verifier-seed-000000000000000000",
            )
            await wallet_cfg.create(self._pool)
            self._verifier = AgentCfg(AgentType.verifier, '_verifier')
            await self._verifier.create(wallet_cfg)
        return self._verifier

    async def _handle_verify_proof(self, request):
        """
        Verify a proof returned by TheOrgBook

        Args:
            request: the request to verify a proof
        """
        verifier = await self._get_verifier()
        result = await verifier.verify_proof(request.proof_req, request.proof)
        parsed_proof = revealed_attrs(request.proof)

        return IndyVerifiedProof(result, parsed_proof)

    async def _handle_ledger_status(self):
        """
        Download the ledger status from von-network and return it to the client
        """
        url = self._ledger_url
        async with self.http as client:
            response = await client.get("{}/status".format(url))
        return await response.text()

    def _agent_http_client(self, agent_id: str = None, **kwargs):
        """
        Create a new :class:`ClientSession` which includes DID signing information in each request

        Args:
            an optional identifier for a specific issuer service (to enable DID signing)
        Returns:
            the initialized :class:`ClientSession` object
        """
        if "request_class" not in kwargs:
            kwargs["request_class"] = SignedRequest
        if agent_id and "auth" not in kwargs:
            kwargs["auth"] = self._did_auth(agent_id)
        return super(IndyService, self).http_client(**kwargs)

    def _did_auth(self, agent_id: str, header_list=None):
        """
        Create a :class:`SignedRequestAuth` representing our authentication credentials,
        used to sign outgoing requests

        Args:
            issuer_id: the unique identifier of the issuer
            header_list: optionally override the list of headers to sign
        """
        agent = self._agents.get(agent_id)
        if not agent:
            raise IndyConfigError("Unknown agent ID: {}".format(agent_id))
        wallet = self._wallets[agent.wallet_id]
        if agent.did and wallet.seed:
            key_id = "did:sov:{}".format(agent.did)
            secret = wallet.seed
            if isinstance(secret, str):
                secret = secret.encode("ascii")
            return SignedRequestAuth(key_id, "ed25519", secret, header_list)
        return None

    async def _service_request(self, request: ServiceRequest) -> ServiceResponse:
        """
        Process a message from the exchange and send the reply, if any

        Args:
            message: the message to be processed
        """
        if isinstance(request, LedgerStatusReq):
            text = await self._handle_ledger_status()
            reply = LedgerStatus(text)

        elif isinstance(request, RegisterAgentReq):
            try:
                agent_id = self._add_agent(request.agent_type, request.wallet_id, **request.config)
                reply = self._get_agent_status(agent_id)
                self.run_task(self._sync())
            except IndyError as e:
                reply = IndyServiceFail(str(e))

        elif isinstance(request, RegisterConnectionReq):
            try:
                connection_id = self._add_connection(
                    request.connection_type, request.agent_id, **request.config)
                reply = self._get_connection_status(connection_id)
                self.run_task(self._sync())
            except IndyError as e:
                reply = IndyServiceFail(str(e))

        elif isinstance(request, RegisterCredentialTypeReq):
            try:
                self._add_credential_type(
                    request.issuer_id,
                    request.schema_name,
                    request.schema_version,
                    request.origin_did,
                    request.attr_names,
                    request.config)
                reply = IndyServiceAck()
            except IndyError as e:
                reply = IndyServiceFail(str(e))

        elif isinstance(request, RegisterWalletReq):
            try:
                wallet_id = self._add_wallet(**request.config)
                reply = self._get_wallet_status(wallet_id)
                self.run_task(self._sync())
            except IndyError as e:
                reply = IndyServiceFail(str(e))

        elif isinstance(request, AgentStatusReq):
            reply = self._get_agent_status(request.agent_id)

        elif isinstance(request, ConnectionStatusReq):
            reply = self._get_connection_status(request.connection_id)

        elif isinstance(request, WalletStatusReq):
            reply = self._get_wallet_status(request.wallet_id)

        elif isinstance(request, IssueCredentialReq):
            try:
                reply = await self._issue_credential(
                    request.connection_id,
                    request.schema_name,
                    request.schema_version,
                    request.origin_did,
                    request.cred_data)
            except IndyError as e:
                reply = IndyServiceFail(str(e))

        #elif isinstance(request, IndyVerifyProofReq):
        #    reply = await self._handle_verify_proof(request)

        else:
            reply = None
        return reply
