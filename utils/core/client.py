import asyncio
import random

from web3.contract import AsyncContract
from web3.exceptions import TransactionNotFound, TimeExhausted
from web3 import AsyncHTTPProvider, AsyncWeb3

from .network import Network
from .logger import logger
from data.config import ERC20_ABI, NETWORK_TOKEN_CONTRACTS


class BlockchainException(Exception):
    pass

class SoftwareException(Exception):
    pass


class Client():
    def __init__(
            self, network: Network, private_key: str, name: str,
            proxy: None | str = None, ds_auth_token: None | str = None
        ):
        self.name = name
        self.network: Network = network
        self.proxy_init = proxy
        self.rpc = random.choice(self.network.rpc)
        self.request_kwargs = {"proxy": f'{proxy}', "verify_ssl": False} if proxy else {"verify_ssl": False}
        self.w3 = AsyncWeb3(AsyncHTTPProvider(self.rpc, request_kwargs=self.request_kwargs))
        self.private_key = private_key
        self.address = AsyncWeb3.to_checksum_address(self.w3.eth.account.from_key(private_key).address)

    @staticmethod
    def get_user_agent():
        random_version = f"{random.uniform(520, 540):.2f}"
        return (f'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/{random_version}'
                f' (KHTML, like Gecko) Chrome/121.0.0.0 Safari/{random_version} Edg/121.0.0.0')

    @staticmethod
    def get_normalize_error(error: Exception) -> Exception | str:
        try:
            if isinstance(error.args[0], dict):
                error = error.args[0].get('message', error)
            return error
        except:
            return error

    async def change_rpc(self):
        logger.warning(f'Замена RPS | {self.address}')

        if len(self.network.rpc) != 1:
            rpcs_list = [rpc for rpc in self.network.rpc if rpc != self.rpc]
            new_rpc = random.choice(rpcs_list)
            self.w3 = AsyncWeb3(AsyncHTTPProvider(new_rpc, request_kwargs=self.request_kwargs))
            logger.success(f'Замена RPS прошла успешно! | {self.address}')
        else:
            logger.error(f'Не удалось сменить RPS | {self.address}')

    def get_contract(self, contract_address: str, abi: dict = ERC20_ABI) -> AsyncContract:
        return self.w3.eth.contract(
            address=AsyncWeb3.to_checksum_address(contract_address),
            abi=abi
        )
        
    async def get_decimals(self, token_name: str = None) -> int:
        contract = await self.get_contract(NETWORK_TOKEN_CONTRACTS[token_name])
        return await contract.functions.decimals().call()
    
    async def get_normalize_amount(self, token_name: str, amount_in_wei: int) -> float:
        decimals = await self.get_decimals(token_name)
        return float(amount_in_wei / 10 ** decimals)
    
    async def get_token_balance(self, token: str = None, check_native: bool = False) -> int | None:
        if check_native:
            token_balance = await self.w3.eth.get_balance(self.address)
            return token_balance or None
        contract = await self.get_contract(NETWORK_TOKEN_CONTRACTS[token])
        token_balance = await contract.functions.balanceOf(self.address).call()
        return token_balance or None
    
    async def get_allowance(self, token_address: str, spender_address: str) -> int:
        contract = self.get_contract(token_address)
        return await contract.functions.allowance(
            self.address,
            spender_address
        ).call()
    
    async def get_priotiry_fee(self):
        fee_history = await self.w3.eth.fee_history(25, 'latest', [20.0])
        non_empty_block_priority_fees = [fee[0] for fee in fee_history["reward"] if fee[0] != 0]

        divisor_priority = max(len(non_empty_block_priority_fees), 1)

        priority_fee = int(round(sum(non_empty_block_priority_fees) / divisor_priority))

        return priority_fee
    
    async def prepare_transaction(self, value: int = 0):
        try:
            tx_params = {
                'from': self.address,
                'nonce': await self.w3.eth.get_transaction_count(self.address),
                'value': value,
                'chainId': self.network.chain_id
            }
            if self.network.eip1559_support:
                base_fee = await self.w3.eth.gas_price
                max_priority_fee_per_gas = await self.get_priotiry_fee()
                max_fee_per_gas = base_fee + max_priority_fee_per_gas
                tx_params['maxPriorityFeePerGas'] = max_priority_fee_per_gas
                tx_params['maxFeePerGas'] = int(max_fee_per_gas * 1.5)
                tx_params['type'] = '0x2'
            else:
                tx_params['gasPrice'] = int(await self.w3.eth.gas_price * 1.5)

            return tx_params
        except Exception as error:
            raise BlockchainException(f'{self.get_normalize_error(error)}')
        
    async def send_transaction(self, transaction, need_hash: bool = False, without_gas: bool = False,
                               poll_latency: int = 10, timeout: int = 360):
        try:
            if not without_gas:
                transaction['gas'] = int((await self.w3.eth.estimate_gas(transaction)) * 1.5)
        except Exception as error:
            raise BlockchainException(f'{self.get_normalize_error(error)}')

        try:
            singed_tx = self.w3.eth.account.sign_transaction(transaction, self.private_key)
            tx_hash = await self.w3.eth.send_raw_transaction(singed_tx.raw_transaction)
        except Exception as error:
            if self.get_normalize_error(error) == 'already known':
                logger.warning(f'RPC получил ошибку, но tx был отправлен | {self.address}')
                return True
            else:
                raise BlockchainException(f'{self.get_normalize_error(error)}')

        try:
            total_time = 0
            timeout = 1200

            while True:
                try:
                    receipts = await self.w3.eth.get_transaction_receipt(tx_hash) 
                    status = receipts.get("status")
                    if status == 1:
                        logger.success(f'Транзакция прошла успешно: {self.network.explorer}tx/{tx_hash.hex()} | {self.address}')
                        if need_hash:
                            return tx_hash
                        return True
                    elif status is None:
                        await asyncio.sleep(poll_latency)
                    else:
                        return SoftwareException(f'Транзакция не выполнилась: {self.network.explorer}tx/{tx_hash.hex()}')
                except TransactionNotFound:
                    if total_time > timeout:
                        raise TimeExhausted(f"Транзакция отсутствует в цепочке после {timeout} секунд")
                    total_time += poll_latency
                    await asyncio.sleep(poll_latency)

                except Exception as error:
                    logger.error(f'RPC получил автоматический ответ. Ошибка: {error} | {self.address}')
                    total_time += poll_latency
                    await asyncio.sleep(poll_latency)
        except Exception as error:
            raise BlockchainException(f'{self.get_normalize_error(error)}')
