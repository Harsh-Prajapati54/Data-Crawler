from huggingface_hub import HfApi

api = HfApi()

api.upload_file(
    path_or_fileobj=r"C:\Users\HarshPrajapati\Downloads\github-code-crawler-v0.2.0\github-code-crawler\data\code.txt",
    path_in_repo="code.txt",
    repo_id="harshpr674/python-code",
    repo_type="dataset"
)