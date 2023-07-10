from celery import shared_task
from datetime import datetime
from ethstakersclub.settings import BEACON_API_ENDPOINT
import json
from ethstakersclub.settings import DEPOSIT_CONTRACT_ADDRESS, BEACON_API_ENDPOINT, SLOTS_PER_EPOCH, \
    EXECUTION_HTTP_API_ENDPOINT, w3, SECONDS_PER_SLOT, MEV_BOOST_RELAYS, MAX_SLOTS_PER_DAY, GENESIS_TIMESTAMP, \
    EPOCH_REWARDS_HISTORY_DISTANCE
import requests
from blockfetcher.models import Block, Validator, Withdrawal, AttestationCommittee, ValidatorBalance, EpochReward, StakingDeposit
from blockfetcher.models import Epoch, SyncCommittee, MissedSync, MissedAttestation
import decimal
from itertools import islice
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime
import binascii
from django.db.models import Sum
from django.utils import timezone
import time
from django.db.models import Q, Count, Func, F
import logging
from django.core.cache import cache
from collections import Counter
import timeout_decorator
from blockfetcher.beacon_api import BeaconAPI
from django.db import transaction
from blockfetcher.task_process_validators import process_validators


beacon = BeaconAPI(BEACON_API_ENDPOINT)
logger = logging.getLogger(__name__)


@shared_task(bind=True, soft_time_limit=25, max_retries=10000, acks_late=True, reject_on_worker_lost=True, acks_on_failure_or_timeout=True)
def load_block_task(self, slot, epoch):
    try:
        load_block(slot, epoch)
    except Exception as e:
        logger.warning("An error occurred processing the slot %s.", slot, exc_info=True)
        self.retry(countdown=5)


@shared_task(bind=True, soft_time_limit=60, max_retries=10000, acks_late=True, reject_on_worker_lost=True, acks_on_failure_or_timeout=True)
def load_epoch_task(self, epoch, slot):
    try:
        load_epoch(epoch, slot)
    except Exception as e:
        logger.warning("An error occurred processing the epoch %s.", slot, exc_info=True)
        self.retry(countdown=5)


@shared_task(bind=True, soft_time_limit=10200, max_retries=10000, acks_late=True, reject_on_worker_lost=True, acks_on_failure_or_timeout=True)
def load_epoch_rewards_task(self, epoch):
    try:
        load_epoch_rewards(epoch)
    except Exception as e:
        logger.warning("An error occurred while processing the epoch rewards for epoch %s.", epoch, exc_info=True)

        self.retry(countdown=5)
    

@shared_task(bind=True, soft_time_limit=300, max_retries=10000, acks_late=True, reject_on_worker_lost=True, acks_on_failure_or_timeout=True)
def get_deposits_task(self, fromBlock, toBlock):
    try:
        get_deposits(fromBlock, toBlock)
    except Exception as e:
        logger.warning("An error occurred while processing the deposits from block %s to %s.", fromBlock, toBlock, exc_info=True)

        self.retry(countdown=5)


@shared_task(bind=True, soft_time_limit=30, max_retries=10000, acks_late=True, reject_on_worker_lost=True, acks_on_failure_or_timeout=True)
def fetch_mev_rewards_task(self, lowest_slot, cursor_slot):
    try:
        fetch_mev_rewards(lowest_slot, cursor_slot)
    except Exception as e:
        logger.warning("An error occurred while fetching MEV rewards for slot %s.", cursor_slot, exc_info=True)
        self.retry(countdown=5)


@shared_task(bind=True, soft_time_limit=800, max_retries=10000, acks_late=True, reject_on_worker_lost=True, acks_on_failure_or_timeout=True)
def epoch_aggregate_missed_attestations_and_average_mev_reward_task(self, epoch):
    try:
        epoch_aggregate_missed_attestations_and_average_mev_reward(epoch)
    except Exception as e:
        logger.warning("An error occurred while aggregating the missed attestations for epoch %s.", epoch, exc_info=True)
        self.retry(countdown=5)


@shared_task(bind=True, soft_time_limit=380, max_retries=10000, acks_late=True, reject_on_worker_lost=True, acks_on_failure_or_timeout=True)
def process_validators_task(self, slot):
    try:
        process_validators(slot)
    except Exception as e:
        logger.warning("An error occurred while updating the validators %s.", slot, exc_info=True)
        self.retry(countdown=5)


@shared_task(bind=True, soft_time_limit=960, max_retries=10000, acks_late=True, reject_on_worker_lost=True, acks_on_failure_or_timeout=True)
def make_balance_snapshot_task(self, slot, timestamp):
    try:
        make_balance_snapshot(slot, timestamp)
    except Exception as e:
        logger.warning("An error occurred while updating the validators %s.", slot, exc_info=True)
        self.retry(countdown=5)


def fetch_mev_rewards(lowest_slot, cursor_slot):
    logger.info("Fetch MEV rewards  lowest_slot=%s cursor_slot=%s", lowest_slot, cursor_slot)

    all_rewards = {}
    for key, value in MEV_BOOST_RELAYS.items():
        retry_count = 0
        while retry_count < 3:
            try:
                sl = cursor_slot
                while sl >= lowest_slot:
                    url = value + "/relay/v1/data/bidtraces/proposer_payload_delivered?limit=100&cursor=" + str(sl)
                    rewards = requests.get(url).json()

                    if len(rewards) == 0:
                        sl = -1
                        break

                    for r in rewards:
                        if int(r["slot"]) < lowest_slot:
                            sl = -1
                            break
                        if int(r["slot"]) not in all_rewards:
                            all_rewards[int(r["slot"])] = {"slot": int(r["slot"]), "value": decimal.Decimal(int(r["value"])),
                                                        "relay": [], "mev_reward_recipient": r["proposer_fee_recipient"]}
                        all_rewards[int(r["slot"])]["relay"].append(key)

                        if int(r["slot"]) - 1 < sl:
                            sl = int(r["slot"]) - 1
                break
            except Exception as e:
                logger.warning(f"An error occurred ({value}):", e)
                retry_count += 1

    blocks_to_update = []
    for key, r in all_rewards.items():
        blocks_to_update.append(
            Block(slot_number=int(r["slot"]), mev_reward=decimal.Decimal(int(r["value"])), 
                  mev_boost_relay=r["relay"], total_reward=decimal.Decimal(int(r["value"])),
                  mev_reward_recipient=r["mev_reward_recipient"]
                 )
        )

    Block.objects.bulk_update(blocks_to_update, fields=["mev_reward", "mev_boost_relay", "total_reward", "mev_reward_recipient"])


@transaction.atomic
def make_balance_snapshot(slot, timestamp):
    timestamp = timezone.make_aware(datetime.fromtimestamp(int(timestamp)), timezone=timezone.utc).date()
    timestamp_target = timestamp - timezone.timedelta(days=1)

    lowest_slot_at_date = Block.objects.filter(slot_number__range=(slot - (MAX_SLOTS_PER_DAY + 300), slot + (MAX_SLOTS_PER_DAY + 300)), timestamp__gt=timestamp)\
        .order_by('slot_number').first().slot_number
    lowest_slot_at_date_target = Block.objects.filter(slot_number__range=(slot - (MAX_SLOTS_PER_DAY + 300), slot + (MAX_SLOTS_PER_DAY + 300)), timestamp__gt=timestamp_target)\
        .order_by('slot_number').first().slot_number
    
    if ValidatorBalance.objects.filter(date=timestamp_target).exists():
        logger.info("snapshot exists on date " + str(timestamp_target) + " slot " + str(lowest_slot_at_date))

        if ValidatorBalance.objects.filter(slot=lowest_slot_at_date).exists():
            logger.info("snapshot slot matches existing one, exiting: %s.", lowest_slot_at_date)
            return
        else:
            logger.error("snapshot slot does not match existing one. new: %s.", lowest_slot_at_date)

    validators = beacon.get_validators(state_id=str(lowest_slot_at_date))

    logger.info("updating historical balance of " + str(len(validators["data"])) + " validators at slot " + str(lowest_slot_at_date))

    # Query to aggregate the total amount withdrawn for each validator
    withdrawal_totals = Withdrawal.objects.filter(block__slot_number__lt=lowest_slot_at_date).values('validator').annotate(total_withdrawn=Sum('amount'))
    total_amount_withdrawn = {withdrawal['validator']: withdrawal['total_withdrawn'] for withdrawal in withdrawal_totals}

    validator_missed_attestations = MissedAttestation.objects.filter(slot__lt=lowest_slot_at_date, slot__gte=lowest_slot_at_date_target).values('validator_id').annotate(count=Count('validator_id'))
    validator_missed_attestations_dict = {v['validator_id']: v['count'] for v in validator_missed_attestations}

    validator_missed_sync = MissedSync.objects.filter(slot__lt=lowest_slot_at_date, slot__gte=lowest_slot_at_date_target).values('validator_id').annotate(count=Count('validator_id'))
    validator_missed_sync_dict = {v['validator_id']: v['count'] for v in validator_missed_sync}

    execution_totals = Block.objects.filter(slot_number__lte=lowest_slot_at_date, empty=0).values(
        'proposer').annotate(execution_total=Sum('total_reward'))

    total_execution_rewards = {block['proposer']: block['execution_total'] for block in
                            execution_totals}

    create_validator_balances_iter = iter(
        ValidatorBalance(validator_id=int(validator["index"]),
                        date=timestamp_target,
                        balance=decimal.Decimal(int(validator["balance"])),
                        total_consensus_balance=decimal.Decimal(
                                                    int(validator["balance"]) +
                                                    (total_amount_withdrawn[int(validator["index"])] if int(validator["index"]) in total_amount_withdrawn else 0)
                                                ),
                        slot=lowest_slot_at_date,
                        execution_reward=decimal.Decimal(
                                                    (total_execution_rewards[int(validator["index"])] if int(validator["index"]) in total_execution_rewards else 0)
                                                ),
                        missed_attestations=validator_missed_attestations_dict[int(validator["index"])] if int(validator["index"]) in validator_missed_attestations_dict else 0,
                        missed_sync=validator_missed_sync_dict[int(validator["index"])] if int(validator["index"]) in validator_missed_sync_dict else 0,
                        )
        for count, validator in enumerate(validators["data"]))

    batch_size = 512

    def insert_batch(batch):
        ValidatorBalance.objects.bulk_create(batch, batch_size, update_conflicts=True, 
                                                update_fields=["total_consensus_balance", "slot", "balance", "execution_reward", "missed_attestations", "missed_sync"], 
                                                unique_fields=["validator_id", "date"])

    pool = ThreadPoolExecutor(max_workers=4)
    futures = []
    while True:
        batch = list(islice(create_validator_balances_iter, batch_size))
        if len(batch) == 0:
            break

        future = pool.submit(insert_batch, batch)
        futures.append(future)
    wait(futures)
    pool.shutdown()


@transaction.atomic
def epoch_aggregate_missed_attestations_and_average_mev_reward(epoch):
    logger.info("calculate average mev reward %s.", epoch)

    if Block.objects.filter(slot_number__gte=(epoch + 1)*SLOTS_PER_EPOCH, slot_number__lt=(epoch + 2)*SLOTS_PER_EPOCH, empty=3).exists():
        raise RuntimeError(f"Block in epoch_aggregate_missed_attestations_and_average_mev_reward still not processed, waiting...")

    blocks = Block.objects.filter(slot_number__gte=epoch*SLOTS_PER_EPOCH, slot_number__lt=(epoch + 1)*SLOTS_PER_EPOCH).values("total_reward", "empty", "slot_number")
    epoch_total_block_reward = 0
    epoch_total_proposed_blocks = 0
    highest_block_reward = 0
    for b in blocks:
        if b["empty"] == 0:
            epoch_total_proposed_blocks += 1
            epoch_total_block_reward += b["total_reward"]

            if b["total_reward"] > highest_block_reward:
                highest_block_reward = b["total_reward"]
        elif b["empty"] == 3:
            raise RuntimeError(f"Block {b['slot_number']} in epoch_aggregate_missed_attestations_and_average_mev_reward still not processed, waiting...")
    average_block_reward = epoch_total_block_reward / epoch_total_proposed_blocks if epoch_total_proposed_blocks > 0 else 0

    logger.info("aggregate missed attestations for epoch %s.", epoch)

    missed_attestations = AttestationCommittee.objects.filter(slot__gte=epoch*SLOTS_PER_EPOCH, slot__lt=(epoch + 1)*SLOTS_PER_EPOCH).values("slot", "epoch", "index", "missed_attestation", "distance")
    existing_missed_attestations_for_epoch = list(MissedAttestation.objects\
        .filter(slot__gte=epoch*SLOTS_PER_EPOCH, slot__lt=(epoch + 1)*SLOTS_PER_EPOCH)\
        .values_list("validator_id", flat=True))

    missed_attestation_to_create = []
    total_attestations = 0
    for m in missed_attestations:
        total_attestations += len(m["distance"])
        if m["missed_attestation"] != None:
            for val in m["missed_attestation"]:
                missed_attestation_to_create.append(MissedAttestation(slot=m["slot"], epoch=m["epoch"], index=m["index"], validator_id=val))
                if val in existing_missed_attestations_for_epoch:
                    existing_missed_attestations_for_epoch.remove(val)

    missed_attestation_count = len(missed_attestation_to_create)
    participation_percent = (total_attestations - missed_attestation_count) / total_attestations * 100 if total_attestations > 0 else 0

    epoch_object = Epoch.objects.get(epoch=epoch)
    epoch_object.participation_percent = participation_percent
    epoch_object.missed_attestation_count = missed_attestation_count
    epoch_object.total_attestations = total_attestations
    epoch_object.epoch_total_proposed_blocks = epoch_total_proposed_blocks
    epoch_object.highest_block_reward = highest_block_reward
    epoch_object.average_block_reward = average_block_reward
    epoch_object.timestamp = timezone.make_aware(datetime.fromtimestamp(GENESIS_TIMESTAMP + (SECONDS_PER_SLOT * epoch * SLOTS_PER_EPOCH)), timezone=timezone.utc)

    logger.info("creating validator statistics %s.", epoch)
    validators = beacon.get_validators(state_id=str(epoch*SLOTS_PER_EPOCH))

    ACTIVE_STATUSES = frozenset({"active_ongoing", "active_exiting", "active_slashed"})
    PENDING_STATUSES = frozenset({"pending_queued", "pending_initialized"})
    EXITED_STATUSES = frozenset({"exited_unslashed", "exited_slashed", "withdrawal_possible", "withdrawal_done"})
    EXITING_STATUSES = frozenset({"active_exiting", "active_slashed"})

    status_list = [v["status"] for v in validators["data"]]
    status_counts = Counter(status_list)

    epoch_object.active_validators = sum(status_counts[status] for status in ACTIVE_STATUSES)
    epoch_object.pending_validators = sum(status_counts[status] for status in PENDING_STATUSES)
    epoch_object.exited_validators = sum(status_counts[status] for status in EXITED_STATUSES)
    epoch_object.exiting_validators = sum(status_counts[status] for status in EXITING_STATUSES)
    epoch_object.validators_status_json = dict(status_counts)

    epoch_object.save()

    MissedAttestation.objects.bulk_create(missed_attestation_to_create, batch_size=1024, ignore_conflicts=True)
    if len(existing_missed_attestations_for_epoch) > 0:
        logger.warning("removing previously wrongly added missed attestations: count= %s", len(existing_missed_attestations_for_epoch))
        MissedAttestation.objects.filter(validator_id__in=existing_missed_attestations_for_epoch).delete()


@transaction.atomic
def load_block(slot, epoch):
    url = BEACON_API_ENDPOINT + "/eth/v1/beacon/blocks/" + str(slot)
    block = requests.get(url).json()
    logger.info("Process block at slot %s: %s", slot, str(block)[:200])

    if "code" in block and block["code"] == 500:
        logger.warning("api error loading block at slot " + str(slot) + ": retrying in 5 seconds")
        time.sleep(5)
        load_block(slot, epoch)
        return

    block_not_found = "message" in block and str(block["message"]) == "NOT_FOUND: beacon block at slot " + str(slot)

    if block_not_found:
        state_root = None
    else:
        state_root = block["data"]["message"]["state_root"]

    try:
        new_block = Block.objects.get(slot_number=int(slot))
    except:
        new_block = Block.objects.create(slot_number=int(slot), epoch=int(epoch))

    if block_not_found and new_block.state_root != state_root:
        logger.warning("potential reorg at slot " + str(slot))
        new_block.empty = 2
        new_block.save()
        return
    elif block_not_found:
        logger.info("block at slot " + str(slot) + " not found (likely not proposed)")
        new_block.empty = 1
        new_block.save()
        return
    elif not block_not_found and new_block.state_root != state_root:
        new_block.empty = 0
        new_block.state_root = state_root
        new_block.deposit_count = int(block["data"]["message"]["body"]["eth1_data"]["deposit_count"])
        new_block.proposer = int(block["data"]["message"]["proposer_index"])

        new_block.parent_root = block["data"]["message"]["parent_root"]
        new_block.signature = block["data"]["signature"]
        new_block.graffiti = block["data"]["message"]["body"]["graffiti"]
        new_block.randao_reveal = block["data"]["message"]["body"]["randao_reveal"]

        url = BEACON_API_ENDPOINT + "/eth/v1/beacon/blocks/" + str(slot) + "/root"
        new_block.block_root = requests.get(url).json()["data"]["root"]

        if "sync_aggregate" in block["data"]["message"]["body"]:
            new_block.sync_committee_signature = block["data"]["message"]["body"]["sync_aggregate"]["sync_committee_signature"]
            new_block.sync_committee_bits = block["data"]["message"]["body"]["sync_aggregate"]["sync_committee_bits"]

            MissedSync.objects.filter(slot=slot).delete()

            sync_period = epoch / 256
            sync_committee = SyncCommittee.objects.get(period=sync_period)

            # Convert hex string to binary representation
            hex_str = block["data"]["message"]["body"]["sync_aggregate"]["sync_committee_bits"]
            binary_string = bin(int(int.from_bytes(bytes.fromhex(hex_str[2:]), byteorder="little")))[2:].zfill(
                len(hex_str[2:]) * 4)[::-1]

            validators_sync_missed = []
            len_binary_string = len(binary_string)

            # Iterate through the binary string and set array elements
            for count, i in enumerate(range(len_binary_string)):
                if binary_string[i] == '0':
                    validators_sync_missed.append(MissedSync(validator_id=sync_committee.validator_ids[count], period=sync_period, slot=slot))

            MissedSync.objects.bulk_create(validators_sync_missed, ignore_conflicts=True)
        if "execution_payload" in block["data"]["message"]["body"] and block["data"]["message"]["body"]["execution_payload"]["parent_hash"] != "0x0000000000000000000000000000000000000000000000000000000000000000":
            new_block.block_hash = block["data"]["message"]["body"]["execution_payload"]["block_hash"]
            new_block.fee_recipient = block["data"]["message"]["body"]["execution_payload"]["fee_recipient"]

            execution_block = w3.eth.get_block(new_block.block_hash, full_transactions=True)

            if "withdrawals" in block["data"]["message"]["body"]["execution_payload"]:
                existing_withdrawals = Withdrawal.objects.filter(block__slot_number=slot)
                if existing_withdrawals.exists():
                    existing_withdrawals.delete()

                withdrawals = block["data"]["message"]["body"]["execution_payload"]["withdrawals"]
                withdrawal_objects = [
                    Withdrawal(
                        index=withdrawal["index"],
                        amount=decimal.Decimal(withdrawal["amount"]),
                        validator=int(withdrawal["validator_index"]),
                        address=withdrawal["address"],
                        block=new_block,
                    )
                    for withdrawal in withdrawals
                ]
                Withdrawal.objects.bulk_create(withdrawal_objects)
                '''
                validator_ids = [int(withdrawal["validator_index"]) for withdrawal in withdrawals]
                validator_total_withdrawals = (
                    Withdrawal.objects.filter(validator__in=validator_ids)
                    .values("validator")
                    .annotate(total=Sum("amount"))
                )

                validator_updates = [
                    Validator(validator_id=withdrawal["validator"], total_withdrawn=decimal.Decimal(withdrawal["total"]))
                    for withdrawal in validator_total_withdrawals
                ]
                Validator.objects.bulk_update(validator_updates, fields=["total_withdrawn"])
                '''

            new_block.block_number = execution_block["number"]
            new_block.timestamp = timezone.make_aware(datetime.fromtimestamp(execution_block["timestamp"]), timezone=timezone.utc)
            parent_hash = "0x" + binascii.hexlify(execution_block["parentHash"]).decode()
            new_block.parent_hash = str(parent_hash)

            if len(execution_block["transactions"]) != 0:
                block_reward = get_block_reward(execution_block, block)
                new_block.total_reward = block_reward["total_reward"]
                new_block.fee_reward = block_reward["total_reward"]
                new_block.total_tx_fee = block_reward["total_tx_fee"]
                new_block.burnt_fee = block_reward["burnt_fee"]
            else:
                new_block.total_reward = 0
                new_block.fee_reward = 0
                new_block.total_tx_fee = 0
                new_block.burnt_fee = 0
            new_block.transaction_count = len(execution_block["transactions"])
        
        attestations = block["data"]["message"]["body"]["attestations"]

        if len(attestations) > 0:
            combinations = [
                {'slot': attestation["data"]["slot"], 'index': attestation["data"]["index"]}
                for attestation in attestations
            ]

            q_objects = Q()

            for combination in combinations:
                q_objects |= Q(**combination)
            
            except_count = 0
            while True:
                try:
                    attestation_committees_ = AttestationCommittee.objects.select_for_update().filter(q_objects)
                    break
                except:
                    if except_count < 4:
                        logger.warning("lock detected, waiting...")
                        time.sleep(1)
                        except_count += 1
                        continue
                    else:
                        logger.warning("deadlock detected")
                        raise

            attestation_committee_dict = {}
            for ac in attestation_committees_:
                attestation_committee_dict[(ac.slot, ac.index)] = ac

            attestation_committee_update = []
            for attestation in attestations:
                if (int(attestation["data"]["slot"]), int(attestation["data"]["index"])) not in attestation_committee_dict:
                    raise Exception("AttestationCommittee missing")
                else:
                    attestation_committee = attestation_committee_dict[(int(attestation["data"]["slot"]), int(attestation["data"]["index"]))]

                # Convert hex string to binary representation
                hex_str = attestation["aggregation_bits"]
                binary_string = bin(int(int.from_bytes(bytes.fromhex(hex_str[2:]), byteorder="little")))[2:].zfill(
                    len(hex_str[2:]) * 4)[::-1]

                distance = list(attestation_committee.distance)
                if len(distance) == 0:
                    distance = [255] * len(binary_string)

                validators_attestation_missed = []
                len_distance = len(distance)

                # Iterate through the binary string and set array elements
                for count, i in enumerate(range(len_distance)):
                    if binary_string[i] == '1':
                        dist = slot - int(attestation["data"]["slot"]) - 1
                        if dist < distance[i]:
                            distance[i] = dist
                    elif distance[i] == 255:
                        validators_attestation_missed.append(attestation_committee.validator_ids[count])

                attestation_committee.distance = list(distance)
                attestation_committee.missed_attestation = list(validators_attestation_missed)
                attestation_committee_update.append(attestation_committee)
            AttestationCommittee.objects.bulk_update(attestation_committee_update, fields=["distance", "missed_attestation"])

        new_block.save()


def send_request_post(what, data):
    url = BEACON_API_ENDPOINT + what
    headers = {
        'accept': 'application/json',
        'Content-Type': 'application/json'
    }

    return requests.post(url, headers=headers, data=data).json()


def load_epoch_rewards(epoch):
    logger.info("load epoch " + str(epoch) + " consensus rewards")

    if EpochReward.objects.filter(epoch=int(epoch)).exists():
        return

    with transaction.atomic():
        attestation_rewards = send_request_post('/eth/v1/beacon/rewards/attestations/' + str(epoch), '[]')
            
        sync_rewards = {}
        for slot in range(epoch * SLOTS_PER_EPOCH, (epoch + 1) * SLOTS_PER_EPOCH):
            sync_rewards_json = send_request_post('/eth/v1/beacon/rewards/sync_committee/' + str(slot), '[]')
            block_not_found = "message" in sync_rewards_json and str(sync_rewards_json["message"]) == "NOT_FOUND: beacon block at slot " + str(slot)

            if not block_not_found:
                for sync_reward in sync_rewards_json["data"]:
                    validator_index = int(sync_reward["validator_index"])
                    reward = int(sync_reward["reward"])

                    if reward >= 0:
                        if validator_index in sync_rewards:
                            sync_rewards[validator_index]["reward"] += reward
                        else:
                            sync_rewards[validator_index] = {"reward": reward, "penalty": 0}
                    else:
                        reward *= -1
                        if validator_index in sync_rewards:
                            sync_rewards[validator_index]["penalty"] += reward
                        else:
                            sync_rewards[validator_index] = {"reward": 0, "penalty": reward}

        epoch_rewards = {}
        for reward in attestation_rewards["data"]["total_rewards"]:
            epoch_rewards[int(reward["validator_index"])] = EpochReward(
                validator_id=int(reward["validator_index"]),
                epoch=epoch,
                attestation_head=reward["head"],
                attestation_target=reward["target"],
                attestation_source=reward["source"],
                sync_reward=sync_rewards[int(reward["validator_index"])]["reward"] if int(reward["validator_index"]) in sync_rewards else None,
                sync_penalty=sync_rewards[int(reward["validator_index"])]["penalty"] if int(reward["validator_index"]) in sync_rewards else None,
            )
        
        for slot in range(epoch * SLOTS_PER_EPOCH, (epoch + 1) * SLOTS_PER_EPOCH):
            url = BEACON_API_ENDPOINT + "/eth/v1/beacon/rewards/blocks/" + str(slot)
            block_reward = requests.get(url).json()
            block_not_found = "message" in block_reward and str(block_reward["message"]) == "NOT_FOUND: beacon block at slot " + str(slot)

            if not block_not_found:
                epoch_rewards[int(block_reward["data"]["proposer_index"])].block_attestations = int(block_reward["data"]["attestations"])
                epoch_rewards[int(block_reward["data"]["proposer_index"])].block_sync_aggregate = int(block_reward["data"]["sync_aggregate"])
                epoch_rewards[int(block_reward["data"]["proposer_index"])].block_proposer_slashings = int(block_reward["data"]["proposer_slashings"])
                epoch_rewards[int(block_reward["data"]["proposer_index"])].block_attester_slashings = int(block_reward["data"]["attester_slashings"])

        EpochReward.objects.bulk_create(epoch_rewards.values(), batch_size=512, update_conflicts=True, 
                                        update_fields=["attestation_head", "attestation_target", "attestation_source", "sync_reward", 
                                                               "sync_penalty", "block_attestations", "block_sync_aggregate", "block_proposer_slashings", 
                                                               "block_attester_slashings"], 
                                                unique_fields=["validator_id", "epoch"])

    logger.info(f"delete epoch {epoch} old consensus rewards")

    epoch_reward_objects_to_delete = EpochReward.objects.filter(epoch__lt=epoch-EPOCH_REWARDS_HISTORY_DISTANCE)

    if epoch_reward_objects_to_delete.exists():
        first_pk_to_delete = epoch_reward_objects_to_delete.first().pk
        last_pk_to_delete = epoch_reward_objects_to_delete.last().pk
        batch_size = 50000
        last_deleted = 0
        for i in range(first_pk_to_delete, last_pk_to_delete - batch_size, batch_size):
            EpochReward.objects.filter(pk__gte=i, pk__lte=i+batch_size).delete()
            last_deleted = i
        EpochReward.objects.filter(pk__gte=last_deleted, pk__lte=last_pk_to_delete).delete()


def get_block_reward(execution_block, block):
    total_tx_fee = 0
    headers = {"Content-Type": "application/json"}

    data = [{
        "jsonrpc": "2.0",
        "method": "eth_getTransactionReceipt",
        "params": ["0x" + binascii.hexlify(t.hash).decode()],
        "id": count
    } for count, t in enumerate(execution_block["transactions"])]

    receipts = requests.post(EXECUTION_HTTP_API_ENDPOINT, json=data, headers=headers).json()
    for r in receipts:
        tx_fee = int(execution_block["transactions"][int(r["id"])]["gasPrice"]) * int(r["result"]["gasUsed"], 16)
        total_tx_fee += tx_fee

    base_fee = int(block["data"]["message"]["body"]["execution_payload"]["base_fee_per_gas"])
    burnt_fee = base_fee * execution_block.gasUsed
    total_reward = total_tx_fee - burnt_fee

    return {"total_reward": total_reward, "total_tx_fee": total_tx_fee, "burnt_fee": burnt_fee}


def get_deposits(fromBlock, toBlock):
    deposit_contract = w3.eth.contract(address=DEPOSIT_CONTRACT_ADDRESS, abi=json.loads('[{"inputs":[],"stateMutability":"nonpayable","type":"constructor"},{"anonymous":false,"inputs":[{"indexed":false,"internalType":"bytes","name":"pubkey","type":"bytes"},{"indexed":false,"internalType":"bytes","name":"withdrawal_credentials","type":"bytes"},{"indexed":false,"internalType":"bytes","name":"amount","type":"bytes"},{"indexed":false,"internalType":"bytes","name":"signature","type":"bytes"},{"indexed":false,"internalType":"bytes","name":"index","type":"bytes"}],"name":"DepositEvent","type":"event"},{"inputs":[{"internalType":"bytes","name":"pubkey","type":"bytes"},{"internalType":"bytes","name":"withdrawal_credentials","type":"bytes"},{"internalType":"bytes","name":"signature","type":"bytes"},{"internalType":"bytes32","name":"deposit_data_root","type":"bytes32"}],"name":"deposit","outputs":[],"stateMutability":"payable","type":"function"},{"inputs":[],"name":"get_deposit_count","outputs":[{"internalType":"bytes","name":"","type":"bytes"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"get_deposit_root","outputs":[{"internalType":"bytes32","name":"","type":"bytes32"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"bytes4","name":"interfaceId","type":"bytes4"}],"name":"supportsInterface","outputs":[{"internalType":"bool","name":"","type":"bool"}],"stateMutability":"pure","type":"function"}]'))

    deposit_filter = deposit_contract.events.DepositEvent.create_filter(fromBlock=fromBlock, toBlock=toBlock)
    logs = deposit_filter.get_all_entries()

    deposits_to_create = []
    for log in logs:
        deposits_to_create.append(StakingDeposit(
            index=int.from_bytes(log.args.index, byteorder='little'),
            block_number=int(log.blockNumber),
            amount=int.from_bytes(log.args.amount, byteorder='little'),
            public_key=log.args.pubkey.hex(),
            withdrawal_credentials=log.args.withdrawal_credentials.hex(),
            signature=log.args.signature.hex(),
            transaction_index=int(log.transactionIndex),
            transaction_hash=log.transactionHash.hex(),
        ))
    StakingDeposit.objects.bulk_create(deposits_to_create, 512, update_conflicts=True, 
                                                update_fields=["block_number", "amount", "public_key", "withdrawal_credentials", 
                                                               "signature", "transaction_index", "transaction_hash"], 
                                                unique_fields=["index"])


@timeout_decorator.timeout(50)
@transaction.atomic
def load_epoch(epoch, slot):
    logger.info("load proposals at epoch " + str(epoch) + " slot " + str(slot))

    epoch_proposer = beacon.get_proposer_duties(epoch)

    new_epoch, created = Epoch.objects.get_or_create(epoch=int(epoch),
                                                     defaults={'dependent_root': epoch_proposer["dependent_root"],
                                                               'timestamp': timezone.make_aware(datetime.fromtimestamp(GENESIS_TIMESTAMP + (SECONDS_PER_SLOT * epoch * SLOTS_PER_EPOCH)), timezone=timezone.utc)})
    
    dependent_root_not_match = new_epoch.dependent_root != epoch_proposer["dependent_root"]

    if not created and dependent_root_not_match:
        logger.warning("epoch already exists")
        new_epoch.dependent_root = epoch_proposer["dependent_root"]
        new_epoch.save()

        AttestationCommittee.objects.filter(epoch=epoch).delete()
    if created or dependent_root_not_match:
        proposals_epoch = [
            Block(
                slot_number=int(proposal["slot"]),
                epoch=epoch,
                proposer=proposal["validator_index"],
                timestamp=timezone.make_aware(datetime.fromtimestamp(GENESIS_TIMESTAMP + (SECONDS_PER_SLOT * int(proposal["slot"]))), timezone=timezone.utc)
            )
            for proposal in epoch_proposer["data"]
        ]
        Block.objects.bulk_create(proposals_epoch, batch_size=512, update_conflicts=True, update_fields=["proposer"], unique_fields=["slot_number"])

        logger.info("load attestation committee at epoch " + str(epoch) + " slot " + str(slot))
        url = BEACON_API_ENDPOINT + '/eth/v1/beacon/states/' + str(slot) + '/committees?epoch=' + str(epoch)
        epoch_attestations = requests.get(url).json()

        logger.info(f"Bulk create AttestationCommittee for epoch {epoch}")

        attestation_committees = [
            AttestationCommittee(
                slot=int(committee["slot"]),
                index=int(committee["index"]),
                epoch=epoch,
                validator_ids=committee["validators"],
                distance=[255]*len(committee["validators"])
            )
            for committee in epoch_attestations["data"]
        ]

        AttestationCommittee.objects.bulk_create(attestation_committees, batch_size=10000, ignore_conflicts=True)
