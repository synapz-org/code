import os
import pickle
import atexit
import weakref
import bittensor as bt
from typing import List
from pydantic import BaseModel

from .tracker import gather_all_trackers

from coding.finetune.score import score
from coding.schemas.context import Context
from coding.constants import COMPETITION_ID
from coding.rewards.codesim import CodeSimModel
from coding.schemas.tracking import TrackingInfo

from coding.tasks.bigcodebench import BigCodeBenchTask
from coding.datasets.bigcodebench import BigCodeBenchDataset

class FinetuneEventResults(BaseModel):
    trackers: List[TrackingInfo]
    competition_id: str = COMPETITION_ID
    
    def __state_dict__(self):
        return {
            "trackers": [tracker.model_dump() for tracker in self.trackers],
            "competition_id": self.competition_id,
        }


def generate_bigcode_tasks(ds: BigCodeBenchDataset, n: int = 1000) -> List[BigCodeBenchTask]:
    tasks = []
    for _ in range(n):
        tasks.append(BigCodeBenchTask(context=Context(**ds.get())))
    return tasks

def bittensor_injector(self):
    self.wallet = bt.wallet(config=self.config)
    self.dendrite = bt.dendrite(wallet=self.wallet)
    self.subtensor = bt.subtensor(config=self.config)
    self.metagraph = self.subtensor.metagraph(self.config.netuid)


class FinetunePipeline:
    def __init__(self, config, competition_id: str, code_sim_model: CodeSimModel = CodeSimModel()):
        self.config = config
        bittensor_injector(self)
        self.competition_id = competition_id
        self.code_sim_model = code_sim_model
        self.trackers: List[TrackingInfo] = []
        self.dataset = BigCodeBenchDataset()
        self.load_tasks()
        self.load_results()

        # Register cleanup to be called when the object is deleted
        self._finalizer = weakref.finalize(self, self.cleanup)

    def load_tasks(self):
        if os.path.exists(f"tasks_{self.competition_id}.pkl"):
            with open(f"tasks_{self.competition_id}.pkl", "rb") as f:
                self.tasks = pickle.load(f)
        else:
            self.tasks = generate_bigcode_tasks(self.dataset, self.config.finetune_test_size)
            self.store_tasks()
            
    def load_results(self):
        results_file = f"results_{self.competition_id}.pkl"
        if os.path.exists(results_file):
            with open(results_file, "rb") as f:
                saved_results = pickle.load(f)
                self.trackers = saved_results.get("trackers", [])
            
    @property
    def results(self) -> FinetuneEventResults:
        return FinetuneEventResults(trackers=self.trackers, competition_id=self.competition_id)

    def evaluate(self) -> FinetuneEventResults:
        # gather all models
        bt.logging.info("Gathering all models...")
        new_trackers = gather_all_trackers(self)
        bt.logging.info(f"Gathered {len(new_trackers)} trackers.")
        
        for tracking_info in new_trackers:
            
            # Check if the model has already been scored
            previous_score = next((tracker.score for tracker in self.trackers if tracker.model.model_name == tracking_info.model.model_name), None)
            if previous_score is not None:
                bt.logging.info(f"Using previously evaluated score for model: {tracking_info.model}")
                tracking_info.score = previous_score
                continue
            
            if tracking_info in self.trackers:
                bt.logging.info(f"Skipping already evaluated model: {tracking_info.model}")
                continue
            
            bt.logging.info(f"Evaluating model: {tracking_info.model}")
            
            # ensure competition_id is equal to current competition_id
            if tracking_info.model.competition_id != COMPETITION_ID:
                tracking_info.score = 0.0
                continue
            
            model_score = score(
                self, tracking_info.model.model_name, self.tasks, self.code_sim_model
            )
            bt.logging.info(f"Model score from FinetunePipeline: {model_score}")
            tracking_info.score = model_score
            
            # Save intermediate results after each model evaluation
            self.trackers.append(tracking_info)
            self.store_results()

        bt.logging.info(f"All scores from FinetunePipeline: {[tracker.score for tracker in self.trackers]}")
        return self.results
    
    def get_top_model(self) -> TrackingInfo:
        # Zip scores with models and sort by score
        return sorted(self.results, key=lambda x: x.score, reverse=True)[0]

    def __str__(self):
        return f"{self.__class__.__name__}(models={self.trackers!r})"

    def __repr__(self):
        return self.__str__()

    def __state_dict__(self):
        return {
            "trackers": [tracker.model_dump() for tracker in self.trackers],
        }
    
    def store_tasks(self):
        with open(f"tasks_{self.competition_id}.pkl", "wb") as f:
            pickle.dump(self.tasks, f)
            
    def store_results(self):
        with open(f"results_{self.competition_id}.pkl", "wb") as f:
            pickle.dump({
                "trackers": self.trackers
            }, f)
    
    def cleanup(self):
        """
        Delete the tasks file and any other task files
        """
        os.remove(f"tasks_{self.competition_id}.pkl")
        # check if tasks_*.pkl exists and delete it if it does
        for file in os.listdir("."):
            if file.startswith("tasks_") and file.endswith(".pkl"):
                os.remove(file)
            if file.startswith("results_") and file.endswith(".pkl"):
                os.remove(file)

    @staticmethod
    def start(config, code_sim_model: CodeSimModel = CodeSimModel()) -> FinetuneEventResults:
        pipeline = FinetunePipeline(config, code_sim_model)
        result = pipeline.evaluate()
        pipeline.cleanup()  # Ensure cleanup is called after evaluation
        return result
    
# Register cleanup to be called when the process exits
atexit.register(FinetunePipeline.cleanup)