import os
from typing import List
import json
import triton_python_backend_utils as pb_utils
import numpy as np
import torch

os.environ["HF_HOME"] = "/opt/tritonserver/.hf-cache"
from transformers import (
    pipeline,
    AutoTokenizer,
    AutoModelForCausalLM,
    TextIteratorStreamer,
    BitsAndBytesConfig
)
import huggingface_hub

huggingface_hub.login(token=os.environ.get("HF_TOKEN"))  ## Add your HF credentials


class TritonPythonModel:
    def initialize(self, args):
        cur_path = os.path.abspath(__file__)
        self.model_config = json.loads(args["model_config"])
        self.model_params = self.model_config.get("parameters", {})
        self.max_length = int(
            self.model_params.get("max_length", {}).get("string_value", "1024")
        )
        quant_map = {
            "4bit": {"load_in_4bit": True},
            "8bit": {"load_in_8bit": True},
            "full": {},
        }
        logger = pb_utils.Logger
        quant_level = self.model_params.get("quantize", {}).get("string_value", "")
        logger.log_info(f"Quant level: {quant_level}")
        quant_arg = quant_map.get(quant_level, {})
        if quant_arg and quant_level == "4bit":
            bnb_config = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=torch.float16,
                bnb_4bit_use_double_quant=True
            )
            quant_arg = {"quantization_config": bnb_config}
        hf_model = "meta-llama/Meta-Llama-3-8B-Instruct"
        self.tokenizer = AutoTokenizer.from_pretrained(hf_model)
        self.model = AutoModelForCausalLM.from_pretrained(
            hf_model,
            torch_dtype=torch.float16,
            device_map="auto",
            cache_dir=os.environ["HF_HOME"],
            **quant_arg,
        )
        self.model.resize_token_embeddings(len(self.tokenizer))
        self.pipeline = pipeline(
            "text-generation",
            model=self.model,
            tokenizer=self.tokenizer,
            torch_dtype=torch.float16,
            device_map="auto",
        )
        self.pipeline.tokenizer.pad_token_id = self.model.config.eos_token_id

    def generate(self, prompts: List[List[dict]]):
        logger = pb_utils.Logger
        batches = self.pipeline(
            prompts,
            do_sample=True,
            top_k=10,
            num_return_sequences=1,
            eos_token_id=self.tokenizer.eos_token_id,
            pad_token_id=self.tokenizer.eos_token_id,
            max_length=self.max_length,
            batch_size=len(prompts),
        )
        output_tensors = []

        for i, batch in enumerate(batches):
            texts = []
            for i, seq in enumerate(batch):
                text = seq["generated_text"][-1]["content"]
                tokens = self.tokenizer.encode(text)
                logger.log_info(
                    f"Processed item. Number of output tokens: {len(tokens)}"
                )
                texts.append(text)

            tensor = pb_utils.Tensor("generated_text", np.array(text, dtype=np.object_))
            output_tensors.append(tensor)

        return output_tensors

    def _read_tensor(self, request, tensor_name):
        msgs = pb_utils.get_input_tensor_by_name(request, tensor_name).as_numpy()
        msgs = msgs[0][0].decode("utf-8")
        return msgs

    def _make_prompt(self, request):
        sys_msg = self._read_tensor(request, "system_message")
        user_msg = self._read_tensor(request, "user_message")
        prompt_dict = [
            {"role": "system", "content": sys_msg},
            {"role": "user", "content": user_msg},
        ]
        return prompt_dict

    def execute(self, requests: List):
        logger = pb_utils.Logger
        logger.log_info("Llama Received request")
        logger.log_info(f"(Llama) Num prompts in batch: {len(requests)}")
        prompts = [self._make_prompt(request) for request in requests]
        tensor_results = self.generate(prompts)
        responses = [
            pb_utils.InferenceResponse(output_tensors=[tensor])
            for tensor in tensor_results
        ]

        return responses

    def finalize(self):
        print("Cleaning up...")
